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
  LLM_API_KEY      only for hosted APIs (Ollama ignores it)
  LLM_TEMPERATURE  default 0, so runs are as repeatable as the model allows
  LLM_SEED         default 0

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
API_KEY = os.environ.get("LLM_API_KEY", "")
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0"))
SEED = int(os.environ.get("LLM_SEED", "0"))
_TOOL_OUTPUT_CHARS = 4000  # per tool result fed back to the model


def _log(msg):
    print(f"[llm] {msg}", file=sys.stderr, flush=True)


def _chat(messages, timeout, tools=None, json_mode=False):
    body = {"model": MODEL, "messages": messages, "temperature": TEMPERATURE, "seed": SEED}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    start = time.time()
    try:
        resp = requests.post(f"{BASE_URL}/chat/completions", json=body, headers=headers, timeout=timeout)
    finally:
        elapsed = time.time() - start
        record_llm(elapsed * 1000.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    usage = data.get("usage") or {}
    _log(f"{elapsed:.2f}s, {usage.get('prompt_tokens')} prompt / {usage.get('completion_tokens')} completion tokens")
    return data["choices"][0]["message"]


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


def call_gemini_json(prompt, system_instruction=None, response_schema=None, timeout=85):
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
    timeout=175,
    final_response_schema=None,
    final_instruction=None,
):
    deadline = time.time() + timeout
    tools = _openai_tools(tools_declarations)
    messages = [{"role": "system", "content": system_instruction or ""}, {"role": "user", "content": prompt}]

    names = {t["function"]["name"] for t in tools}
    for _ in range(max_tool_rounds + 1):
        msg = _chat(messages, max(deadline - time.time(), 5), tools=tools)
        calls = msg.get("tool_calls") or _text_tool_calls(msg.get("content"), names)
        if not calls:
            _log(f"answer: {(msg.get('content') or '')[:300]!r}")
            break
        if not msg.get("tool_calls"):
            _log(f"tool call(s) written as text, recovered: {[c['function']['name'] for c in calls]}")
            msg["content"] = ""
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = call["function"].get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) else args
                out = execute_tool_fn(name, args)
            except Exception as e:  # bad arguments or a failed call: let the model see it
                out = {"error": str(e)}
            _log(f"tool {name}({json.dumps(args)[:200]}) -> {str(out)[:200]}")
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name,
                             "content": json.dumps(out)[:_TOOL_OUTPUT_CHARS]})
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
    return _parse_json(_chat(messages, max(deadline - time.time(), 5), json_mode=True).get("content"))
