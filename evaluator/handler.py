"""
Evaluator function.

Input (JSON body):
{
  "goal": "...",
  "plan": [ {"id": 1, "description": "..."}, ... ],
  "history": [ {"step_id": 1, "result": "...", "status": "completed"}, ... ]
}

Output (JSON body):
{
  "verdict": "done" | "continue" | "replan",
  "feedback": "<reasoning / what's missing / why it failed>",
  "next_step_id": <int or null>
}
"""
import json
from . import telemetry
from .gemini_client import call_gemini_json

SYSTEM_INSTRUCTION = """You are the EVALUATOR in a planner-actor-evaluator agent loop.
You will be given the goal, the full plan, and the history of steps executed so far
with their results. Decide whether:
  - the goal has now been fully achieved ("done"),
  - execution should continue with the next unexecuted step ("continue"), or
  - the plan itself is flawed/insufficient and needs to be redone ("replan").

Be strict: only say "done" if the history actually demonstrates the goal was met.
"""

EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["done", "continue", "replan"],
        },
        "feedback": {
            "type": "string",
            "description": "Short explanation, and on replan: what the new plan must fix.",
        },
        "next_step_id": {
            "type": ["integer", "null"],
            "description": "Id of the next step to run, or null if done/replan.",
        },
    },
    "required": ["verdict", "feedback", "next_step_id"],
}


def handle(event, context):
    telemetry.begin()  # t3
    try:
        payload = _parse_body(event.body)
        goal = payload.get("goal")
        plan = payload.get("plan")
        history = payload.get("history", [])

        if not goal or not plan:
            return _resp(400, {"error": "Required fields: 'goal', 'plan'"})

        prompt = (
            f"Goal: {goal}\n"
            f"Full plan: {json.dumps(plan)}\n"
            f"History of executed steps: {json.dumps(history)}\n"
        )

        result = call_gemini_json(
            prompt,
            system_instruction=SYSTEM_INSTRUCTION,
            response_schema=EVALUATION_SCHEMA,
        )

        if result.get("verdict") not in ("done", "continue", "replan"):
            return _resp(502, {"error": "Evaluator LLM returned unexpected verdict", "raw": result})

        return _resp(200, result)

    except Exception as e:
        return _resp(500, {"error": str(e)})


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
