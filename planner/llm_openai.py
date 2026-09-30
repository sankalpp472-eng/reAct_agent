"""
Real-LLM backend for any OpenAI-compatible chat API: Ollama (local), vLLM,
Groq, OpenRouter, ... Same public API as the mock in gemini_client.py:

    call_gemini_json(prompt, system_instruction=None, response_schema=None, timeout=85)
    call_gemini_agentic(prompt, tools_declarations, execute_tool_fn, ...)

gemini_client.py switches to these when LLM_BACKEND=openai, so the handlers
don't change. Keep this file identical in planner/, actor/ and evaluator/.

Environment variables:
  LLM_BACKEND      "openai" to use this backend (default "mock")
  LLM_BASE_URL     e.g. http://172.17.0.1:11434/v1 (Ollama); must end in /v1
  LLM_MODEL        e.g. qwen2.5:3b
  LLM_API_KEY      only for hosted APIs (Ollama ignores it). On faasd put it in the
                   OpenFaaS secret "llm-api-key" instead (read from
                   /var/openfaas/secrets/llm-api-key); on Knative it comes from the
                   Kubernetes secret llm-api-key as this env var
  LLM_MAX_RETRIES  retries on 429 (rate limit) / 500 / 502 / 503 / 504, default 6.
                   Waits Retry-After if the API sends it, else 1, 2, 4 ... 30 s.
                   The wait counts toward T_LLM (wall time spent on the model) and
                   is also recorded on its own as _timing.llm_wait_ms
  LLM_PROXY        proxy for the model API only, e.g. socks5h://10.0.2.2:1080 on the
                   cluster VM (its only way out is the node's SSH SOCKS tunnel).
                   Calls to the tool functions never use it. SOCKS needs PySocks
                   (in requirements.txt)
  LLM_TEMPERATURE  default 0, so runs are as repeatable as the model allows
  LLM_SEED         default 0
  LLM_TOOL_MODE    "prompt" (default): the tool list goes into the system
                   prompt in Qwen/Hermes <tools>/<tool_call> format and this
                   file parses the calls from the model's text. Works with
                   any model and avoids Ollama's tool parser, which drops a
                   small model's slightly malformed calls (empty reply, no
                   tool_calls). "native": send tools via the API's `tools`
                   field (hosted APIs with reliable tool calling).

Every HTTP call to the model counts toward T_LLM (telemetry.record_llm), like
the mock's sleeps did, so all the experiment metrics keep working.

With Ollama, set OLLAMA_CONTEXT_LENGTH=8192 (or more) on the server: the
actor's prompt carries all of a domain's tool declarations, and Ollama's
default context would silently cut it off.
"""
import json
import os
import re
import sys
import time

import requests

from .telemetry import record_llm

BASE_URL = os.environ.get("LLM_BASE_URL", "http://172.17.0.1:11434/v1").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "qwen2.5:3b")
_KEY_FILE = os.environ.get("LLM_API_KEY_FILE", "/var/openfaas/secrets/llm-api-key")
API_KEY = os.environ.get("LLM_API_KEY") or (
    open(_KEY_FILE).read().strip() if os.path.exists(_KEY_FILE) else "")
_PROXY = os.environ.get("LLM_PROXY", "")
PROXIES = {"http": _PROXY, "https": _PROXY} if _PROXY else None
MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "6"))
_RETRY_STATUS = (429, 500, 502, 503, 504)
_json_mode_ok = True  # set to False once the API says the model has no JSON mode
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0"))
SEED = int(os.environ.get("LLM_SEED", "0"))
TOOL_MODE = os.environ.get("LLM_TOOL_MODE", "prompt")
_TOOL_OUTPUT_CHARS = 4000  # per tool result fed back to the model
_TRANSCRIPT_CHARS = 600    # per tool output kept in the actor's result, like the mock


def _log(msg):
    print(f"[llm] {msg}", file=sys.stderr, flush=True)


def _chat(messages, timeout, tools=None, json_mode=False):
    """One chat completion. If the model rejects JSON mode (some hosted models
    do), ask again without it: the prompt already requests JSON and _parse_json
    tolerates code fences and stray text."""
    global _json_mode_ok
    try:
        return _chat_once(messages, timeout, tools, json_mode and _json_mode_ok)
    except _JsonModeUnsupported:
        _json_mode_ok = False
        _log("model has no JSON mode; asking for JSON in the prompt only")
        return _chat_once(messages, timeout, tools, False)


class _JsonModeUnsupported(RuntimeError):
    pass


def _chat_once(messages, timeout, tools, json_mode):
    body = {"model": MODEL, "messages": messages, "temperature": TEMPERATURE, "seed": SEED}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    start = time.time()
    deadline = start + timeout
    retries, waited = 0, 0.0
    try:
        while True:
            resp = requests.post(f"{BASE_URL}/chat/completions", json=body, headers=headers,
                                 proxies=PROXIES, timeout=max(deadline - time.time(), 1))
            if resp.status_code not in _RETRY_STATUS or retries >= MAX_RETRIES:
                break
            wait = _retry_after(resp) or min(2 ** retries, 30)
            if time.time() + wait >= deadline:
                break  # no time left for another attempt: report this error
            retries += 1
            waited += wait
            _log(f"HTTP {resp.status_code}, retry {retries}/{MAX_RETRIES} in {wait:.1f}s")
            time.sleep(wait)
    finally:
        elapsed = time.time() - start
        # the wall time includes rate-limit waits; they're also reported on
        # their own (llm_wait_ms) so the metrics can leave them out
        record_llm(elapsed * 1000.0, waited * 1000.0)
    if resp.status_code == 400 and json_mode and "response_format" in resp.text:
        raise _JsonModeUnsupported(resp.text[:300])
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    usage = data.get("usage") or {}
    _log(f"{elapsed:.2f}s ({waited:.1f}s rate-limit wait, {retries} retries), "
         f"{usage.get('prompt_tokens')} prompt / {usage.get('completion_tokens')} completion tokens")
    return data["choices"][0]["message"]


def _retry_after(resp):
    """Seconds to wait from a Retry-After header (Groq, OpenAI and others send it on 429)."""
    try:
        return max(float(resp.headers.get("retry-after", "")), 0.5)
    except ValueError:
        return None


def _schema_hint(schema):
    return ("\n\nRespond with ONLY a JSON object (no markdown, no prose) matching this JSON schema:\n"
            + json.dumps(schema))


def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise RuntimeError(f"LLM did not return JSON: {text[:300]!r}")
        return json.loads(m.group(0))


def call_gemini_json(prompt, system_instruction=None, response_schema=None, timeout=240):
    system = (system_instruction or "") + (_schema_hint(response_schema) if response_schema else "")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    return _parse_json(_chat(messages, timeout, json_mode=True).get("content"))


def _openai_tools(declarations):
    """The tool functions list their tools in Gemini's flat format
    ({type, name, description, parameters}); OpenAI nests them."""
    out = []
    for d in declarations:
        fn = d.get("function") or {k: d[k] for k in ("name", "description", "parameters") if k in d}
        out.append({"type": "function", "function": fn})
    return out


_PROMPT_TOOLS = """

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tools}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{{"name": <function-name>, "arguments": <args-json-object>}}
</tool_call>
Call tools this way until you have what the step needs. When you are done, reply with a short summary and no <tool_call>."""


def _text_tool_calls(content, names):
    """Small models often write a tool call as JSON in their text, e.g.
    {"name": "get_order_details", "arguments": {...}}, sometimes inside
    <tool_call> tags or a code fence, instead of the structured tool_calls
    the server can parse. Recover those, but only for known tool names."""
    calls, text, i = [], content or "", 0
    decoder = json.JSONDecoder()
    while True:
        i = text.find("{", i)
        if i == -1:
            return calls
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        i = end
        for o in obj if isinstance(obj, list) else [obj]:
            if not isinstance(o, dict):
                continue
            o = o.get("function") if isinstance(o.get("function"), dict) else o
            args = o.get("arguments", o.get("parameters", {}))
            if o.get("name") in names and isinstance(args, (dict, str)):
                calls.append({"id": f"text-{len(calls)}", "type": "function", "function": {
                    "name": o["name"], "arguments": args if isinstance(args, str) else json.dumps(args)}})


def call_gemini_agentic(
    prompt,
    tools_declarations,
    execute_tool_fn,
    system_instruction=None,
    max_tool_rounds=6,
    timeout=270,  # under the 300 s function/gateway limits, with rate-limit waits
    final_response_schema=None,
    final_instruction=None,
):
    deadline = time.time() + timeout
    tools = _openai_tools(tools_declarations)
    names = {t["function"]["name"] for t in tools}
    native = TOOL_MODE == "native"
    system = system_instruction or ""
    if not native:
        system += _PROMPT_TOOLS.format(tools="\n".join(json.dumps(t) for t in tools))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    transcript = []  # "name(args) -> output" per tool call, kept verbatim in the result

    for _ in range(max_tool_rounds + 1):
        msg = _chat(messages, max(deadline - time.time(), 5), tools=tools if native else None)
        calls = msg.get("tool_calls") or _text_tool_calls(msg.get("content"), names)
        if not calls:
            _log(f"answer: {(msg.get('content') or '')[:300]!r}")
            break
        if native:
            messages.append({"role": "assistant", "content": "" if not msg.get("tool_calls") else
                             (msg.get("content") or ""), "tool_calls": calls})
        else:
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
        results = []
        for call in calls:
            name = call["function"]["name"]
            args = {}
            try:
                args = call["function"].get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) else args
                out = execute_tool_fn(name, args)
            except Exception as e:  # bad arguments or a failed call: let the model see it
                out = {"error": str(e)}
            _log(f"tool {name}({json.dumps(args)[:200]}) -> {str(out)[:200]}")
            text = out.get("output", out.get("error")) if isinstance(out, dict) else out
            transcript.append(f"{name}({json.dumps(args)}) -> {str(text)[:_TRANSCRIPT_CHARS]}")
            content = json.dumps(out)[:_TOOL_OUTPUT_CHARS]
            if native:
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name,
                                 "content": content})
            else:
                results.append(f"<tool_response>\n{content}\n</tool_response>")
        if results:
            messages.append({"role": "user", "content": "\n".join(results)})
    else:
        _log(f"stopped after {max_tool_rounds} tool rounds")
        msg = {"content": ""}

    if final_response_schema is None:
        return msg.get("content") or ""
    # Final "restate as JSON" turn, as the Gemini client did
    if msg.get("content"):
        messages.append({"role": "assistant", "content": msg["content"]})
    messages.append({"role": "user", "content": (final_instruction or "Restate your answer as JSON only.")
                     + _schema_hint(final_response_schema)})
    answer = _parse_json(_chat(messages, max(deadline - time.time(), 5), json_mode=True).get("content"))
    # A model's summary tends to drop the ids and prices later steps need, so
    # keep the raw tool outputs in the result, in the same form as the mock.
    if transcript and isinstance(answer, dict) and "result" in answer:
        answer["result"] = f"{answer['result']} Tool calls: " + " | ".join(transcript)
    return answer
