"""
Shared Gemini client for planner/actor/evaluator, using Google's Interactions API
(https://ai.google.dev/gemini-api/docs/interactions-overview), which replaced the
older generateContent endpoint. Copy this file into each function's directory.
"""
import os
import json
import requests

MODEL = "gemini-2.5-flash"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"  # pins the request/response schema explicitly


def _api_key():
    # faasd mounts secrets at /var/openfaas/secrets/<name>; some setups use /run/secrets.
    for path in ("/var/openfaas/secrets/gemini-api-key", "/run/secrets/gemini-api-key"):
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    # Fallback for local testing outside faasd
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "Gemini API key not found in secret mount or GEMINI_API_KEY env var"
        )
    return key


def _headers():
    return {
        "x-goog-api-key": _api_key(),
        "Content-Type": "application/json",
        "Api-Revision": API_REVISION,
    }


def _extract_output_text(interaction):
    """The REST API doesn't hand back a convenience `output_text` field (that's
    an SDK-only helper) - reconstruct it by joining the text parts of every
    model_output step. Whitespace-only output (e.g. a stray newline with no
    real content) is treated the same as "no text found" so callers get a
    clear error instead of silently receiving whitespace and failing later
    with a cryptic json.loads error."""
    texts = []
    for step in interaction.get("steps", []):
        if step.get("type") == "model_output":
            for part in step.get("content", []):
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
    joined = "".join(texts).strip()
    return joined if joined else None


def call_gemini_json(prompt, system_instruction=None, response_schema=None, timeout=85):
    """Single-turn call. If response_schema (a JSON Schema dict) is given, uses
    the Interactions API's structured-output mode so the model is constrained
    to emit valid JSON matching it - used by planner and evaluator."""
    body = {"model": MODEL, "input": prompt}
    if system_instruction:
        body["system_instruction"] = system_instruction
    if response_schema:
        body["response_format"] = {
            "type": "text",
            "mime_type": "application/json",
            "schema": response_schema,
        }

    resp = requests.post(API_BASE, headers=_headers(), json=body, timeout=timeout)
    _raise_with_body(resp)
    interaction = resp.json()

    output_text = _extract_output_text(interaction)
    if output_text is None:
        raise RuntimeError(f"Gemini response had no text output: {interaction}")

    return json.loads(output_text)


def _raise_with_body(resp):
    """requests' raise_for_status() only includes the status line, not the
    response body - Gemini's actual error explanation lives in the body, so
    surface it instead of a bare '400 Client Error'."""
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise requests.HTTPError(
            f"{resp.status_code} error calling Gemini: {detail}", response=resp
        )


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
    """Multi-turn tool-calling loop, used by actor. Sends the prompt with tool
    declarations, executes any function_call steps locally via execute_tool_fn,
    and feeds results back using previous_interaction_id chaining (the
    Interactions API manages conversation state server-side) until the model
    stops calling tools and returns final text.

    tools_declarations: list of {"type": "function", "name", "description",
    "parameters"} dicts - see TOOL_DECLARATIONS in tools.py.
    execute_tool_fn: callable(tool_name: str, args: dict) -> JSON-serializable result.

    IMPORTANT: `response_format` (structured/JSON-schema output) and `tools`
    (function calling) cannot both be set on the same request - most tool-
    calling APIs, including this one, drop JSON-mode constraints whenever
    tools are attached, since the model needs freedom to emit function_call
    steps instead of final text. That means a system_instruction saying
    "reply with ONLY JSON" is just a suggestion during the tool-calling
    phase, and the model is free to ignore it (e.g. by returning a nice
    markdown write-up instead of the requested JSON object).

    If final_response_schema is given, once the model stops calling tools we
    make ONE additional follow-up turn - chained via previous_interaction_id,
    with `tools` dropped and `response_format` turned on - asking the model to
    restate its just-given answer in that exact JSON shape. This turn's JSON
    is schema-enforced server-side, so it can't fail the way free-form text
    can. In that case this function returns a parsed dict instead of a raw
    string; pass a custom final_instruction to control the restating prompt.
    """
    body = {"model": MODEL, "input": prompt, "tools": tools_declarations}
    if system_instruction:
        body["system_instruction"] = system_instruction

    resp = requests.post(API_BASE, headers=_headers(), json=body, timeout=timeout)
    _raise_with_body(resp)
    interaction = resp.json()
    interaction_id = interaction.get("id")

    for _ in range(max_tool_rounds):
        function_calls = [
            s for s in interaction.get("steps", []) if s.get("type") == "function_call"
        ]
        if not function_calls:
            break

        # Execute every call from this turn (handles parallel function calls)
        result_steps = []
        for call in function_calls:
            tool_name = call.get("name")
            call_id = call.get("id")
            args = call.get("arguments", {})
            try:
                tool_result = execute_tool_fn(tool_name, args)
            except Exception as e:
                tool_result = {"error": str(e)}
            result_steps.append(
                {
                    "type": "function_result",
                    "name": tool_name,
                    "call_id": call_id,
                    "result": [{"type": "text", "text": json.dumps(tool_result)}],
                }
            )

        follow_up_body = {
            "model": MODEL,
            "previous_interaction_id": interaction_id,
            "tools": tools_declarations,
            "input": result_steps,
        }
        resp = requests.post(API_BASE, headers=_headers(), json=follow_up_body, timeout=timeout)
        _raise_with_body(resp)
        interaction = resp.json()
        interaction_id = interaction.get("id")
    else:
        # The for-loop ran to completion without `break`, i.e. the model was
        # still issuing function_calls after max_tool_rounds full round-trips.
        # Surface this explicitly rather than falling through to
        # _extract_output_text on an interaction that may have no real
        # final-answer text at all.
        raise RuntimeError(
            f"Exceeded max_tool_rounds ({max_tool_rounds}) without the model "
            f"returning a final answer (still issuing tool calls)"
        )

    output_text = _extract_output_text(interaction)
    if output_text is None:
        raise RuntimeError(f"Gemini agentic call ended with no text output: {interaction}")

    if final_response_schema is None:
        return output_text

    # Phase 2: force the free-form answer above into schema-valid JSON. No
    # `tools` here on purpose - that's what lets response_format take effect.
    structure_body = {
        "model": MODEL,
        "previous_interaction_id": interaction_id,
        "input": final_instruction or (
            "Restate your previous answer as JSON only, matching the required schema. "
            "Do not add commentary, markdown, or code fences."
        ),
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": final_response_schema,
        },
    }
    resp = requests.post(API_BASE, headers=_headers(), json=structure_body, timeout=timeout)
    _raise_with_body(resp)
    structured_interaction = resp.json()

    structured_text = _extract_output_text(structured_interaction)
    if structured_text is None:
        raise RuntimeError(
            f"Gemini failed to produce structured JSON on the follow-up turn: "
            f"{structured_interaction}"
        )
    return json.loads(structured_text)
