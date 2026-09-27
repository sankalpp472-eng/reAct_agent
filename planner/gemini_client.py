"""
MOCK Gemini client - drop-in replacement for the real gemini_client.py.

Same public API as the real client:
    call_gemini_json(prompt, system_instruction=None, response_schema=None, timeout=85)
    call_gemini_agentic(prompt, tools_declarations, execute_tool_fn, ...)

so planner/actor/evaluator handlers need NO changes. No network calls, no API
key needed. Every "LLM call" is replaced by a sleep (to simulate inference
latency) followed by a deterministic, pre-scripted answer.

Because OpenFaaS functions are stateless, the mock works out "where we are in
the loop" purely from the prompt the handler builds:

  planner   -> counts completed steps listed in the context ("Steps already
               completed so far ...: [...]") and returns the REMAINING steps
               of a fixed master plan of MOCK_NUM_STEPS steps.
  actor     -> reads "Step to execute now: {...}" and returns a canned result,
               optionally running MOCK_ACTOR_TOOL_ROUNDS fake tool calls
               (calculator only, so no Tavily/network) to exercise the
               tool-calling loop.
  evaluator -> reads the plan + history and returns "done" once every step in
               the plan has a completed entry in history, else "continue" with
               the next pending step id.

Step ids are GLOBAL (1..MOCK_NUM_STEPS) and stay stable across re-plans, so the
history is easy to read and each run is fully reproducible.

Workload scripts: if the goal matches one of the tau-bench workloads in
mock_workloads.py, all three roles follow that workload's script instead of
the generic plan above. The actor then makes the scripted tool calls for real
(through execute_tool_fn -> the retail-tools / airline-tools faasd function,
so the actor request must carry "domain"), one simulated model turn per call,
and the evaluator returns the workload's final answer as its "done" feedback.

Environment variables (set per function in stack.yaml):
  MOCK_LATENCY_S          base seconds to sleep per simulated LLM call  (default 2.0)
  MOCK_LATENCY_JITTER_S   +/- uniform jitter added to each sleep         (default 0.0)
  MOCK_SEED               seed for the jitter RNG (unset = non-deterministic)
  MOCK_NUM_STEPS          planner only: size of the master plan          (default 4)
  MOCK_ACTOR_TOOL_ROUNDS  actor only: fake tool-call round trips         (default 1)

Actor latency mirrors the real client, which makes 1 initial call + 1 call per
tool round + 1 "restate as JSON" call, i.e. (MOCK_ACTOR_TOOL_ROUNDS + 2) sleeps.
"""
import json
import os
import random
import re
import sys
import time

from .mock_workloads import find_workload, script_plan, script_step
from .telemetry import record_llm

MODEL = "mock-gemini"

_LATENCY_S = float(os.environ.get("MOCK_LATENCY_S", "2.0"))
_JITTER_S = float(os.environ.get("MOCK_LATENCY_JITTER_S", "0.0"))
_NUM_STEPS = int(os.environ.get("MOCK_NUM_STEPS", "4"))
_ACTOR_TOOL_ROUNDS = int(os.environ.get("MOCK_ACTOR_TOOL_ROUNDS", "1"))
_TOOL_OUTPUT_CHARS = 600  # per tool output, in the actor's result text
_seed = os.environ.get("MOCK_SEED")
_rng = random.Random(int(_seed)) if _seed is not None else random.Random()

_STEP_TEMPLATES = [
    "Gather background information relevant to the goal",
    "Collect concrete options and data points",
    "Compare the options and compute costs/trade-offs",
    "Select the best option against the goal's constraints",
    "Draft the final answer for the goal",
    "Double-check the final answer for completeness",
]


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _log(msg):
    # stderr shows up in `faas-cli logs <fn>` / journalctl for faasd
    print(f"[mock-llm] {msg}", file=sys.stderr, flush=True)


def _simulate_latency(label):
    delay = max(0.0, _LATENCY_S + _rng.uniform(-_JITTER_S, _JITTER_S))
    start = time.time()
    time.sleep(delay)
    elapsed = time.time() - start
    record_llm(elapsed * 1000.0)  # counts toward T_LLM (see telemetry.py)
    _log(f"{label}: slept {elapsed:.3f}s")
    return delay


def _json_after(marker, text, default=None):
    """Return the JSON value that immediately follows `marker` in `text`."""
    idx = text.find(marker)
    if idx == -1:
        return default
    rest = text[idx + len(marker):].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(rest)
        return value
    except ValueError:
        return default


def _goal(prompt):
    m = re.search(r"^Goal:\s*(.*)$", prompt, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _completed_ids(history):
    ids = set()
    for h in history or []:
        if isinstance(h, dict) and h.get("status") == "completed" and h.get("step_id") is not None:
            ids.add(h["step_id"])
    return ids


def _master_plan():
    plan = []
    for i in range(1, _NUM_STEPS + 1):
        if i <= len(_STEP_TEMPLATES):
            desc = _STEP_TEMPLATES[i - 1]
        else:
            desc = f"Additional work item {i}"
        plan.append({"id": i, "description": desc})
    return plan


# ----------------------------------------------------------------------------
# role detection + scripted answers
# ----------------------------------------------------------------------------

def _detect_role(system_instruction, response_schema):
    si = system_instruction or ""
    if "PLANNER" in si:
        return "planner"
    if "EVALUATOR" in si:
        return "evaluator"
    if "ACTOR" in si:
        return "actor"
    # fallback on schema shape
    props = (response_schema or {}).get("properties", {})
    if "plan" in props:
        return "planner"
    if "verdict" in props:
        return "evaluator"
    return "unknown"


def _planner_answer(prompt):
    goal = _goal(prompt)
    # build_planner_context in the Conductor workflow appends this marker + history JSON
    history = _json_after("only plan what still remains):", prompt, default=[])
    done = _completed_ids(history)
    workload = find_workload(goal)
    master = script_plan(workload) if workload else _master_plan()
    remaining = [s for s in master if s["id"] not in done]
    if not remaining:
        # The real planner handler rejects an empty plan with 502; keep a
        # single wrap-up step so the loop can reach the evaluator instead.
        remaining = [{"id": len(master) + 1, "description": "Confirm the goal is fully achieved"}]
    if workload:
        _log(f"planner: workload {workload['id']}")
    _log(f"planner: {len(done)} completed, returning {len(remaining)} remaining steps")
    return {"goal": goal, "plan": remaining}


def _evaluator_answer(prompt):
    plan = _json_after("Full plan:", prompt, default=[]) or []
    history = _json_after("History of executed steps:", prompt, default=[]) or []
    done = _completed_ids(history)
    pending = [s["id"] for s in plan if s.get("id") not in done]
    workload = find_workload(_goal(prompt))
    last = history[-1] if history and isinstance(history[-1], dict) else {}
    if workload and last.get("status") == "failed":
        # The planner re-plans everything not yet completed, so the failed
        # step comes back as the first step of the new plan.
        verdict = {
            "verdict": "replan",
            "feedback": f"Step {last.get('step_id')} failed, retry it: {last.get('result', '')}",
            "next_step_id": None,
        }
    elif workload and not pending:
        verdict = {"verdict": "done", "feedback": workload["final_answer"], "next_step_id": None}
    elif not pending:
        verdict = {
            "verdict": "done",
            "feedback": f"All {len(plan)} planned step(s) completed (mock).",
            "next_step_id": None,
        }
    else:
        verdict = {
            "verdict": "continue",
            "feedback": f"{len(pending)} step(s) still pending (mock).",
            "next_step_id": pending[0],
        }
    _log(f"evaluator: verdict={verdict['verdict']} next={verdict['next_step_id']}")
    return verdict


def _tool_failed(out):
    # execute_tool_fn returns the tool server's {"tool", "output", "error": bool},
    # or {"error": "<message>"} if the call itself failed.
    return not isinstance(out, dict) or bool(out.get("error"))


def _scripted_actor(prompt, workload, execute_tool_fn, final_response_schema):
    step = _json_after("Step to execute now:", prompt, default={}) or {}
    step_id = step.get("id")
    script = script_step(workload, step_id)
    _simulate_latency(f"actor turn 1 (initial, workload {workload['id']} step {step_id})")

    if script is None:
        # Planner's wrap-up step (see _planner_answer): nothing left to call.
        answer = {"step_id": step_id, "result": workload["final_answer"], "status": "completed"}
    else:
        outputs, failed = [], False
        for i, (name, args) in enumerate(script["calls"]):
            try:
                out = execute_tool_fn(name, args)
            except Exception as e:
                out = {"error": str(e)}
            failed = failed or _tool_failed(out)
            text = out.get("output", out.get("error")) if isinstance(out, dict) else out
            outputs.append(f"{name}({json.dumps(args)}) -> {str(text)[:_TOOL_OUTPUT_CHARS]}")
            _simulate_latency(f"actor turn {i + 2} (after tool call {name})")
        result = (
            "Tool call failed. " if failed else f"{script['result']} "
        ) + "Tool calls: " + " | ".join(outputs)
        answer = {
            "step_id": step_id,
            "result": result,
            "status": "failed" if failed else "completed",
        }
    _log(f"actor: workload {workload['id']} step {step_id} -> {answer['status']}")

    if final_response_schema is None:
        return json.dumps(answer)
    _simulate_latency("actor final structuring turn")
    return answer


def _actor_answer(prompt, tool_results):
    step = _json_after("Step to execute now:", prompt, default={}) or {}
    step_id = step.get("id")
    desc = step.get("description", "")
    result = f"[mock] Executed step {step_id}: {desc}."
    if tool_results:
        result += f" Tool outputs: {json.dumps(tool_results)}"
    return {"step_id": step_id, "result": result, "status": "completed"}


# ----------------------------------------------------------------------------
# public API (same signatures as the real client)
# ----------------------------------------------------------------------------

def call_gemini_json(prompt, system_instruction=None, response_schema=None, timeout=85):
    role = _detect_role(system_instruction, response_schema)
    _simulate_latency(f"{role} call_gemini_json")
    if role == "planner":
        return _planner_answer(prompt)
    if role == "evaluator":
        return _evaluator_answer(prompt)
    raise RuntimeError(f"mock gemini_client: can't tell which role this prompt is for ({role})")


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
    workload = find_workload(_goal(prompt))
    if workload:
        return _scripted_actor(prompt, workload, execute_tool_fn, final_response_schema)

    # Turn 1: initial model call (with tools attached)
    _simulate_latency("actor turn 1 (initial)")

    if _ACTOR_TOOL_ROUNDS > max_tool_rounds:
        raise RuntimeError(
            f"Exceeded max_tool_rounds ({max_tool_rounds}) without the model "
            f"returning a final answer (still issuing tool calls)"
        )

    # Tool rounds: "model" asks for a calculator call, we run it through the
    # real execute_tool_fn (local, no network), then another model turn.
    tool_results = []
    step = _json_after("Step to execute now:", prompt, default={}) or {}
    base = step.get("id") or 1
    for r in range(_ACTOR_TOOL_ROUNDS):
        expr = f"{base} * 100 + {r}"
        try:
            out = execute_tool_fn("calculator", {"expression": expr})
        except Exception as e:
            out = {"error": str(e)}
        tool_results.append({"tool": "calculator", "expression": expr, "output": out})
        _simulate_latency(f"actor turn {r + 2} (after tool round {r + 1})")

    answer = _actor_answer(prompt, tool_results)

    if final_response_schema is None:
        return json.dumps(answer)

    # Final "restate as JSON" turn
    _simulate_latency("actor final structuring turn")
    return answer
