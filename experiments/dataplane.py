"""
Stack-independent Experiment 1 metrics, computed from the functions' own `_timing`
(the data plane) plus the driver's t0/t7. Used by both drivers, so Conductor +
faasd and Argo + Knative are measured with exactly the same definitions.

Why not the orchestrators' own timestamps? Conductor records task times in ms,
but Argo only records node start/finish to the second, which is too coarse for
per-turn overheads of tens of ms. The function-side timestamps are ms-precise on
both stacks.

Input: one record per orchestrator -> function call (planner/actor/evaluator):
  {"name", "t3", "t4", "llm_ms", "tool_calls": [...], "hop_ms": t5 - t2 or None}
hop_ms is the caller-side round trip; Conductor provides it (HTTP task
start/end). Argo doesn't at ms precision, so for those hops Troute is estimated
as the median Troute of this run's actor -> tool calls, which take the same
Knative request path (Kourier -> queue-proxy -> container).

Metrics (all ms):
  Te2e      = t7 - t0
  Twarm     = t4 - t3 per call
  Troute    = hop_ms - Twarm per call (tool calls: always measured)
  Tcycle_k  = t3(planner, turn k+1) - t3(planner, turn k)   "planner-to-planner":
              one full turn including the loop-back; the last turn has no next
              planner call, so it contributes no Tcycle
  Torch_k   = Tcycle_k - sum over turn k's calls of (Twarm + Troute)
  Torch_run = Te2e - sum over all calls of (Twarm + Troute)
              (includes dispatch, the loop-backs and completion - everything
              the orchestrator does outside the function calls)
  Rfriction = (Torch_run + sum Troute (orchestrator hops + tool hops)) / T_LLM

All t3/t4 differences between *different* functions (Tcycle) assume the
functions share a clock, i.e. run on one host / one node.
"""
import statistics


def _median(xs):
    return statistics.median(xs) if xs else None


def dataplane_metrics(calls, t0, t7):
    """Returns (run_row, turn_rows, call_rows)."""
    calls = sorted(calls, key=lambda c: c["t3"])
    tool_calls = [tc for c in calls for tc in c.get("tool_calls", []) if "twarm_ms" in tc]
    route_estimate = _median([tc["troute_ms"] for tc in tool_calls])

    call_rows = []
    turn = 0
    for c in calls:
        if c["name"] == "planner":
            turn += 1
        twarm = c["t4"] - c["t3"]
        if c.get("hop_ms") is not None:
            troute, estimated = c["hop_ms"] - twarm, False
        else:
            troute, estimated = route_estimate, True
        call_rows.append({
            "turn": turn, "kind": "function", "name": c["name"], "t3": c["t3"],
            "twarm_ms": twarm, "troute_ms": troute, "troute_estimated": estimated,
            "llm_ms": c.get("llm_ms", 0.0),
        })
        for tc in c.get("tool_calls", []):
            call_rows.append({
                "turn": turn, "kind": "tool", "name": tc["tool"], "t3": tc.get("t3"),
                "twarm_ms": tc.get("twarm_ms"), "troute_ms": tc.get("troute_ms"),
                "troute_estimated": False, "llm_ms": 0.0,
            })

    fn_rows = [r for r in call_rows if r["kind"] == "function"]
    turns = sorted({r["turn"] for r in fn_rows})
    planner_t3 = {r["turn"]: r["t3"] for r in fn_rows if r["name"] == "planner"}
    turn_rows = []
    for k in turns:
        rows = [r for r in fn_rows if r["turn"] == k]
        busy = sum(r["twarm_ms"] + (r["troute_ms"] or 0.0) for r in rows)
        tcycle = planner_t3[k + 1] - planner_t3[k] if k + 1 in planner_t3 else None
        turn_rows.append({
            "turn": k,
            "tcycle_ms": tcycle,
            "t_llm_ms": sum(r["llm_ms"] for r in rows),
            "busy_ms": busy,  # time inside the turn's function calls incl. routing
            "torch_ms": tcycle - busy if tcycle is not None else None,
        })

    te2e = t7 - t0
    t_llm = sum(r["llm_ms"] for r in fn_rows)
    troute_orch = sum(r["troute_ms"] or 0.0 for r in fn_rows)
    troute_tool = sum(tc["troute_ms"] for tc in tool_calls)
    torch_run = te2e - sum(r["twarm_ms"] for r in fn_rows) - troute_orch
    run_row = {
        "te2e_ms": te2e,
        "n_turns": len(turns),
        "t_llm_ms": t_llm,
        "torch_run_ms": torch_run,
        "troute_orch_ms": troute_orch,
        "troute_tool_ms": troute_tool,
        "troute_orch_estimated": any(r["troute_estimated"] for r in fn_rows),
        "rfriction": (torch_run + troute_orch + troute_tool) / t_llm if t_llm else None,
        "twarm_tool_mean_ms": statistics.fmean([tc["twarm_ms"] for tc in tool_calls]) if tool_calls else None,
        "troute_tool_mean_ms": statistics.fmean([tc["troute_ms"] for tc in tool_calls]) if tool_calls else None,
    }
    return run_row, turn_rows, call_rows


def stats(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {"n": 0}
    p95 = statistics.quantiles(xs, n=20, method="inclusive")[18] if len(xs) > 1 else xs[0]
    return {
        "n": len(xs),
        "mean": round(statistics.fmean(xs), 3),
        "p50": round(statistics.median(xs), 3),
        "p95": round(p95, 3),
        "std": round(statistics.stdev(xs), 3) if len(xs) > 1 else 0.0,
        "min": round(xs[0], 3),
        "max": round(xs[-1], 3),
    }


def summarize(runs, turns, calls):
    """Stats over the runs in `runs` (the caller filters out failed runs)."""
    keys = {(r["workload"], r["run"]) for r in runs}
    turns = [t for t in turns if (t["workload"], t["run"]) in keys]
    calls = [c for c in calls if (c["workload"], c["run"]) in keys]
    tool = [c for c in calls if c["kind"] == "tool"]
    fn = [c for c in calls if c["kind"] == "function"]
    return {
        "runs": len(runs),
        "per_run": {
            "Te2e_ms": stats([r["te2e_ms"] for r in runs]),
            "T_LLM_ms": stats([r["t_llm_ms"] for r in runs]),
            "Torch_run_ms": stats([r["torch_run_ms"] for r in runs]),
            "Troute_orch_ms": stats([r["troute_orch_ms"] for r in runs]),
            "Troute_tool_ms": stats([r["troute_tool_ms"] for r in runs]),
            "Rfriction": stats([r["rfriction"] for r in runs]),
        },
        "per_turn": {
            "Tcycle_ms": stats([t["tcycle_ms"] for t in turns]),
            "Torch_ms": stats([t["torch_ms"] for t in turns]),
        },
        "per_tool_call": {
            "Twarm_ms": stats([c["twarm_ms"] for c in tool]),
            "Troute_ms": stats([c["troute_ms"] for c in tool]),
        },
        "per_function_call": {
            name: {
                "Twarm_ms": stats([c["twarm_ms"] for c in fn if c["name"] == name]),
                "Troute_ms": stats([c["troute_ms"] for c in fn if c["name"] == name]),
            }
            for name in ("planner", "actor", "evaluator")
        },
    }


def print_table(summary, title):
    rows = [
        ("Te2e (per run)", summary["per_run"]["Te2e_ms"]),
        ("Tcycle (per turn)", summary["per_turn"]["Tcycle_ms"]),
        ("Torch (per turn)", summary["per_turn"]["Torch_ms"]),
        ("Torch_run (per run)", summary["per_run"]["Torch_run_ms"]),
        ("Twarm (per tool call)", summary["per_tool_call"]["Twarm_ms"]),
        ("Troute (per tool call)", summary["per_tool_call"]["Troute_ms"]),
        ("Troute orch (per run)", summary["per_run"]["Troute_orch_ms"]),
        ("T_LLM (per run)", summary["per_run"]["T_LLM_ms"]),
        ("Rfriction (per run)", summary["per_run"]["Rfriction"]),
    ]
    print(f"\n{title}")
    print(f"{'metric':<24}{'n':>5}{'mean':>12}{'p50':>12}{'p95':>12}{'std':>12}")
    for name, s in rows:
        if s.get("n"):
            print(f"{name:<24}{s['n']:>5}{s['mean']:>12}{s['p50']:>12}{s['p95']:>12}{s['std']:>12}")
