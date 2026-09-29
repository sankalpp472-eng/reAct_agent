"""
Actor function. Executes ONE step of the plan, using tools when helpful.

Input (JSON body):
{
  "goal": "...",
  "plan": [ {"id": 1, "description": "..."}, ... ],
  "step_id": 1,
  "history": [                     # optional, prior actor results in this run
    {"step_id": 1, "result": "..."}
  ],
  "domain": "retail" | "airline"   # optional, see below
}

Output (JSON body):
{
  "step_id": 1,
  "description": "...",
  "result": "<what the actor did / produced>",
  "status": "completed" | "failed"
}

The actor has three tools available via Gemini function calling:
  - web_search:   live web search (Tavily API) for current/factual information
  - http_request: call a specific, known HTTP endpoint
  - calculator:   safe arithmetic evaluation

If "domain" is set, the actor gets that tau-bench domain's tools instead
(served by the retail-tools / airline-tools function).

See tools.py for the tool implementations and gemini_client.call_gemini_agentic
for the tool-calling loop itself.
"""
import json
import re
from . import telemetry
from .gemini_client import call_gemini_agentic
from .tools import TOOL_DECLARATIONS, TAU_DOMAINS, domain_tools, execute_tool

# Enforced server-side on the post-tool-calling "restate as JSON" turn (see
# gemini_client.call_gemini_agentic's final_response_schema). This is what
# actually guarantees valid JSON - the SYSTEM_INSTRUCTION text below is only
# a hint during the tool-calling phase itself, when response_format can't be
# turned on at the same time as tools.
ACTOR_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "step_id": {"type": "integer"},
        "result": {"type": "string"},
        "status": {"type": "string", "enum": ["completed", "failed"]},
    },
    "required": ["result", "status"],
}

SYSTEM_INSTRUCTION = """You are the ACTOR in a planner-actor-evaluator agent loop.
You will be given the overall goal, the full plan, the history of steps already
completed, and ONE specific step to execute now. Execute just that step and
report the outcome.

You have three tools available:
  - web_search: for current, live, or factual information you're not certain of
  - http_request: to call a specific, known API endpoint
  - calculator: for any arithmetic - never compute numbers yourself, always use this

Use a tool whenever it would make your answer more accurate than reasoning alone.
Do not invent facts you could instead look up or compute.

Once you are done, respond with ONLY a JSON object - no markdown code fences, no
prose outside the JSON - of this exact shape:
{
  "step_id": <int, same as input>,
  "result": "<what you did / found / produced for this step>",
  "status": "completed" or "failed"
}
"""

DOMAIN_INSTRUCTION = """You are the ACTOR in a planner-actor-evaluator agent loop,
working as a {domain} customer service agent. You will be given the customer's
request (the goal), the results of the steps already completed, and ONE
specific step to execute now. Execute just that step and report the outcome.

Use the provided tools to look up and change the customer's data - never invent
ids, prices or other details you could look up. Tools that change data act
immediately, so only call them when this step requires it.

You know nothing about the customer's account, orders, products or bookings
except what a tool returns. If the step needs any such information, call the
tool first; if you have not received a tool result for it, the step is not
done. Report ids, amounts and other details exactly as the tools returned them.
If a tool returns an error, report the step as "failed" with the error.

Work in this order: first call the tool(s) the step needs and read their
results; only then write a short summary of what you did and found. You will
be asked for the final structured answer separately afterwards.
"""


def handle(event, context):
    telemetry.begin()  # t3
    try:
        payload = _parse_body(event.body)
        goal = payload.get("goal")
        plan = payload.get("plan")
        step_id = payload.get("step_id")
        history = payload.get("history", [])
        domain = payload.get("domain") or None

        if not goal or not plan or step_id is None:
            return _resp(400, {"error": "Required fields: 'goal', 'plan', 'step_id'"})
        if domain is not None and domain not in TAU_DOMAINS:
            return _resp(400, {"error": f"'domain' must be one of {list(TAU_DOMAINS)}"})

        step = next((s for s in plan if s.get("id") == step_id), None)
        if step is None:
            return _resp(400, {"error": f"step_id {step_id} not found in plan"})

        prompt = (
            f"Goal: {goal}\n"
            f"Full plan: {json.dumps(plan)}\n"
            f"History so far: {json.dumps(history)}\n"
            f"Step to execute now: {json.dumps(step)}\n"
        )

        max_tool_rounds = 6
        if domain:
            tools_declarations, execute_tool_fn = domain_tools(domain)
            system_instruction = DOMAIN_INSTRUCTION.format(domain=domain)
            # A small model given the whole goal and plan tries to do all of
            # it in one step. Show the goal as background only, leave out the
            # rest of the plan, and make this one step the task.
            prompt = (
                f"Goal: {goal}\n"
                f"(The goal is background only. It is being worked on one step at a time; "
                f"other calls do the other steps.)\n"
                f"Results of earlier steps: {json.dumps(history)}\n"
                f"Step to execute now: {json.dumps(step)}\n\n"
                f"Do ONLY this step. Call only the tool(s) this step needs, then stop and "
                f"summarize what this step found or changed. Do not start later steps.\n"
            )
            max_tool_rounds = 4
        else:
            tools_declarations, execute_tool_fn = TOOL_DECLARATIONS, execute_tool
            system_instruction = SYSTEM_INSTRUCTION

        actor_output = call_gemini_agentic(
            prompt,
            max_tool_rounds=max_tool_rounds,
            tools_declarations=tools_declarations,
            execute_tool_fn=execute_tool_fn,
            system_instruction=system_instruction,
            final_response_schema=ACTOR_RESULT_SCHEMA,
            final_instruction=(
                f"Restate your answer for step {step_id} as JSON only, matching "
                f"the required schema (step_id, result, status). No commentary, "
                f"no markdown, no code fences."
            ),
        )

        # With final_response_schema set, call_gemini_agentic already returns
        # a parsed dict. The isinstance check + _parse_json_loose fallback is
        # just defense in depth in case that contract ever changes upstream.
        result = (
            actor_output
            if isinstance(actor_output, dict)
            else _parse_json_loose(actor_output)
        )

        result.setdefault("step_id", step_id)
        result["description"] = step.get("description")

        return _resp(200, result)

    except Exception as e:
        return _resp(500, {"error": str(e)})


def _parse_json_loose(text):
    """Tool-calling responses aren't in strict JSON mode, so be forgiving
    about how the model wraps its answer:
      1. reject outright empty/whitespace-only text with a clear error
      2. strip accidental markdown code fences (```json ... ``` or ``` ... ```)
      3. if the text still isn't valid JSON on its own (e.g. the model added
         a sentence of prose before/after), fall back to extracting the
         outermost {...} block and parsing that instead
    """
    text = text.strip()
    if not text:
        raise RuntimeError("Actor model returned empty text (no JSON to parse)")

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    if not text:
        raise RuntimeError("Actor model returned only an empty code fence, no JSON body")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"Could not find JSON object in actor output: {text!r}")
        return json.loads(match.group(0))


def _parse_body(body):
    if isinstance(body, (dict, list)):
        return body
    if not body:
        return {}
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def _resp(status_code, body_dict):
    return {"statusCode": status_code, "body": telemetry.attach(body_dict)}
