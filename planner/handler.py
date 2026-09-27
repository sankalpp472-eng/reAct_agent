"""
Planner function.

Input (JSON body):
{
  "goal": "Book a 3-day trip to Lisbon under $800",
  "context": "optional extra context/constraints, may include a summary of
               steps already completed (see Conductor workflow v2)",   # optional
  "feedback": "why the previous plan failed, if any"                    # optional, sent on re-plan
}

Output (JSON body):
{
  "goal": "...",
  "plan": [
    {"id": 1, "description": "..."},
    ...
  ]
}
"""
import json
from .gemini_client import call_gemini_json

SYSTEM_INSTRUCTION = """You are the PLANNER in a planner-actor-evaluator agent loop.
Given a goal (and optional feedback from a previous failed attempt), break it down
into a short, ordered list of concrete, actionable steps that an "actor" agent can
execute one at a time. Keep the plan as short as possible while still complete.

IMPORTANT - handling steps already completed:
The "context" field may include a section describing steps that have ALREADY been
completed successfully (it will be prefixed with something like "Steps already
completed so far"). If present:
  - Treat every step listed there as DONE. Do not include it in your plan again,
    even in a modified or rephrased form.
  - Your plan should contain ONLY the steps still needed, starting from where the
    completed work leaves off, in order to fully reach the goal.
If no such section is present, this is the first plan for this goal - plan the
whole thing from scratch.
"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "description": "The restated goal."},
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Ordinal id for this step, starting at 1."},
                    "description": {"type": "string", "description": "What this step involves."},
                },
                "required": ["id", "description"],
            },
        },
    },
    "required": ["goal", "plan"],
}


def handle(event, context):
    try:
        payload = _parse_body(event.body)
        goal = payload.get("goal")
        if not goal:
            return _resp(400, {"error": "Missing required field: 'goal'"})

        extra_context = payload.get("context", "")
        feedback = payload.get("feedback", "")

        prompt = f"Goal: {goal}\n"
        if extra_context:
            prompt += f"Context: {extra_context}\n"
        if feedback:
            prompt += f"NOTE: Feedback on the current attempt to address in this plan: {feedback}\n"

        result = call_gemini_json(
            prompt,
            system_instruction=SYSTEM_INSTRUCTION,
            response_schema=PLAN_SCHEMA,
        )

        if "plan" not in result or not isinstance(result["plan"], list):
            return _resp(502, {"error": "Planner LLM returned unexpected shape", "raw": result})

        if len(result["plan"]) == 0:
            return _resp(
                502,
                {
                    "error": (
                        "Planner LLM returned an empty plan. If context included "
                        "completed steps, this likely means it believes the goal "
                        "is already fully done - the evaluator should be the one "
                        "to decide that, not an empty plan here."
                    ),
                    "raw": result,
                },
            )

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
    return {"statusCode": status_code, "body": body_dict}
