"""
Print what the agent did in a saved run, turn by turn: each plan, each actor
step (result, status, tools called) and each evaluator verdict. Handy with a
real LLM, to see why a run scored reward 0.

  python experiments/show_run.py experiments/results/exp1-conductor-<ts>/raw/retail-44-run1.json
  python experiments/show_run.py experiments/results/exp1-argo-<ts>/raw/retail-44-run1.json

Works on the raw files of both drivers (Conductor executions and Argo
Workflow objects).
"""
import json
import sys
import textwrap

FUNCTIONS = ("planner", "actor", "evaluator")


def _calls(wf):
    """(name, response body) of every planner/actor/evaluator call, in order."""
    out = []
    if "tasks" in wf:  # Conductor execution
        for t in wf["tasks"]:
            name = (t.get("referenceTaskName") or "").replace("_task", "").split("__")[0]
            response = (t.get("outputData") or {}).get("response")
            body = response.get("body") if isinstance(response, dict) else None
            if t.get("taskType") == "HTTP" and name in FUNCTIONS:
                if not isinstance(body, dict):  # failed task: show why instead of skipping it
                    body = {"error": f"task {t.get('status')}: {t.get('reasonForIncompletion') or response}"}
                out.append((t.get("startTime") or 0, name, body))
    else:  # Argo Workflow
        for n in ((wf.get("status") or {}).get("nodes") or {}).values():
            if n.get("displayName") in FUNCTIONS and n.get("type") == "HTTP":
                try:
                    body = json.loads((n.get("outputs") or {}).get("result") or "")
                except ValueError:
                    continue
                out.append((n.get("startedAt") or "", n["displayName"], body))
    # order by the function's own entry time when present (ms-precise on both stacks)
    # the function's own entry time (ms-precise on both stacks); for a failed
    # call without one, the orchestrator's start time (also epoch ms on Conductor)
    def when(c):
        t3 = (c[2].get("_timing") or {}).get("t3")
        return t3 if t3 else (c[0] if isinstance(c[0], (int, float)) else 0)
    return [(name, body) for _, name, body in sorted(out, key=when)]


def _wrap(text, indent="      "):
    return textwrap.fill(str(text), 100, initial_indent=indent, subsequent_indent=indent)


def main(path):
    with open(path) as f:
        wf = json.load(f)
    turn = 0
    for name, body in _calls(wf):
        timing = body.get("_timing") or {}
        llm = f"{timing.get('llm_ms', 0) / 1000:.1f}s LLM, {timing.get('llm_calls', 0)} calls"
        if name == "planner":
            turn += 1
            print(f"\n=== turn {turn}")
            print(f"  PLANNER ({llm})")
            for s in body.get("plan") or []:
                print(_wrap(f"{s.get('id')}. {s.get('description')}"))
            if body.get("error"):
                print(_wrap(f"ERROR: {body['error']}"))
        elif name == "actor":
            tools = [tc.get("tool") + ("" if "twarm_ms" in tc else " (FAILED)")
                     for tc in timing.get("tool_calls", [])]
            print(f"  ACTOR step {body.get('step_id')}: {body.get('status')} ({llm})")
            print(_wrap(f"tools: {', '.join(tools) or 'none'}"))
            print(_wrap(body.get("result") or body.get("error")))
        elif name == "evaluator":
            print(f"  EVALUATOR: {body.get('verdict')} -> next step {body.get('next_step_id')} ({llm})")
            print(_wrap(body.get("feedback") or body.get("error")))
    output = wf.get("output") or {}
    if output.get("final_answer"):
        print("\nFINAL ANSWER")
        print(_wrap(output["final_answer"], "  "))


if __name__ == "__main__":
    main(sys.argv[1])
