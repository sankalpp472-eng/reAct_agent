"""
Experiment 1 (single-agent baseline, warm tools) for the Conductor + faasd stack.

Runs each workload N times, one at a time (C=1), through the pae_agentic_loop
Conductor workflow, and computes the Experiment 1 latency metrics from the
timestamp pipeline:

  [t0] Dispatch -> [t1] Orch Start -> [t_llm] LLM Call -> [t2] Gateway Ping
    -> [t3] Container Enter -> [t4] Container Exit -> [t5] FaaS Response
    -> [t6] Orch Step End -> [t7] Final Answer Received

Where each timestamp comes from (see experiments/README.md for details):
  t0, t7  this driver, just before starting the workflow / when it sees it finish
  t1, t6  Conductor task times: first task scheduled / last task ended, per loop turn
  t2, t5  the caller: Conductor's HTTP task start/end for planner/actor/evaluator,
          the actor's own clock for calls to retail-tools / airline-tools
  t3, t4  the called function's handler (the `_timing` object in its response)
  T_LLM   the functions' own LLM time (`_timing.llm_ms`)

Metrics:
  Te2e      = t7 - t0                              per run
  Tcycle    = t6 - t1                              per loop turn
  Twarm     = t4 - t3                              per function call
  Troute    = (t5 - t2) - Twarm                    per function call
  Torch     = Tcycle - (T_LLM + T_FaaS_HTTP)       per loop turn, where
              T_LLM + T_FaaS_HTTP = sum of (t5 - t2) over the turn's HTTP tasks
  Rfriction = (sum Torch + sum Troute) / sum T_LLM per run

Usage:
  python experiments/exp1_conductor.py \\
      --conductor http://<conductor-host>:<port>/api \\
      --gateway   http://<faasd-host>:8080 \\
      --runs 10 --warmup 1
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime

# The Conductor API is local; never route it through a proxy set in the shell
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def task_body(task):
    """The function's JSON response body of a Conductor HTTP task, or None.
    A failed task (timeout, connection error) stores a plain string as its
    response instead of a {"body": ...} object."""
    response = (task.get("outputData") or {}).get("response")
    body = response.get("body") if isinstance(response, dict) else None
    return body if isinstance(body, dict) else None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "workloads"))
from score import _call, score  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dataplane  # noqa: E402

WORKFLOW = "pae_agentic_loop"
HTTP_TASKS = ("planner_task", "actor_task", "evaluator_task")


# ---------------------------------------------------------------- Conductor API

def _conductor(method, url, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def start_workflow(conductor, workflow_input):
    # Returns the workflow id as plain text
    return _conductor("POST", f"{conductor}/workflow/{WORKFLOW}", workflow_input).strip().strip('"')


def wait_for_workflow(conductor, wf_id, poll_s, timeout_s):
    """Poll until the workflow leaves RUNNING. Returns (t7, status). t7 is taken
    at the first poll that sees it finished, so it's at most one poll interval
    (plus one GET) late."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = json.loads(
            _conductor("GET", f"{conductor}/workflow/{wf_id}?includeTasks=false")
        )["status"]
        if status not in ("RUNNING", "PAUSED"):
            return time.time() * 1000.0, status
        time.sleep(poll_s)
    return time.time() * 1000.0, "DRIVER_TIMEOUT"


def get_workflow(conductor, wf_id):
    return json.loads(_conductor("GET", f"{conductor}/workflow/{wf_id}?includeTasks=true"))


# ---------------------------------------------------------------- metrics

def _iteration(task):
    if task.get("iteration"):
        return task["iteration"]
    ref = task.get("referenceTaskName", "")
    return int(ref.rsplit("__", 1)[1]) if "__" in ref else 0


def _ref(task):
    return task.get("referenceTaskName", "").split("__")[0]


def compute_metrics(wf, t0, t7):
    """Returns (run_row, cycle_rows, call_rows) for one finished workflow."""
    loop_tasks = [
        t for t in wf.get("tasks", [])
        if _iteration(t) > 0 and t.get("taskType") != "DO_WHILE"
    ]
    cycles = {}
    for t in loop_tasks:
        cycles.setdefault(_iteration(t), []).append(t)

    cycle_rows, call_rows = [], []
    for it in sorted(cycles):
        tasks = cycles[it]
        t1 = min(t["scheduledTime"] for t in tasks)
        t6 = max(t["endTime"] for t in tasks)
        t_llm = http_total = troute = 0.0
        n_tool_calls = 0
        missing_timing = False

        for t in tasks:
            if t.get("taskType") != "HTTP":
                continue
            http_ms = t["endTime"] - t["startTime"]  # t5 - t2, Conductor clock
            http_total += http_ms
            body = task_body(t)
            timing = body.get("_timing") if isinstance(body, dict) else None
            if timing is None:
                missing_timing = True
                continue
            twarm = timing["handler_ms"]  # t4 - t3, function's clock
            t_llm += timing["llm_ms"]
            troute += http_ms - twarm
            call_rows.append({
                "iteration": it, "kind": "function", "name": _ref(t).replace("_task", ""),
                "http_ms": http_ms, "twarm_ms": twarm, "troute_ms": http_ms - twarm,
                "llm_ms": timing["llm_ms"],
            })
            for tc in timing.get("tool_calls", []):
                n_tool_calls += 1
                if "twarm_ms" in tc:
                    troute += tc["troute_ms"]
                call_rows.append({
                    "iteration": it, "kind": "tool", "name": tc["tool"],
                    "http_ms": tc["http_ms"], "twarm_ms": tc.get("twarm_ms"),
                    "troute_ms": tc.get("troute_ms"), "llm_ms": 0.0,
                })

        tcycle = t6 - t1
        cycle_rows.append({
            "iteration": it,
            "tcycle_ms": tcycle,
            "t_llm_ms": t_llm,
            "t_faas_http_ms": http_total - t_llm,
            "torch_ms": tcycle - http_total,  # = Tcycle - (T_LLM + T_FaaS_HTTP)
            "troute_ms": troute,
            "n_tool_calls": n_tool_calls,
            "missing_timing": missing_timing,
        })

    t_llm_total = sum(c["t_llm_ms"] for c in cycle_rows)
    torch_total = sum(c["torch_ms"] for c in cycle_rows)
    troute_total = sum(c["troute_ms"] for c in cycle_rows)
    tool_calls = [c for c in call_rows if c["kind"] == "tool" and c["twarm_ms"] is not None]
    run_row = {
        "status": wf.get("status"),
        "te2e_ms": t7 - t0,
        # engine-side view of the same run (Conductor clock), for sanity checks
        "te2e_engine_ms": (wf.get("endTime") or 0) - (wf.get("createTime") or 0),
        "n_cycles": len(cycle_rows),
        "tcycle_sum_ms": sum(c["tcycle_ms"] for c in cycle_rows),
        "t_llm_ms": t_llm_total,
        "t_faas_http_ms": sum(c["t_faas_http_ms"] for c in cycle_rows),
        "torch_ms": torch_total,
        "troute_ms": troute_total,
        "rfriction": (torch_total + troute_total) / t_llm_total if t_llm_total else None,
        "n_tool_calls": len(tool_calls),
        "twarm_tool_mean_ms": _mean([c["twarm_ms"] for c in tool_calls]),
        "troute_tool_mean_ms": _mean([c["troute_ms"] for c in tool_calls]),
        "missing_timing": any(c["missing_timing"] for c in cycle_rows),
    }
    return run_row, cycle_rows, call_rows


def _mean(xs):
    return statistics.fmean(xs) if xs else None


def _stats(xs):
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


def summarize(runs, cycles, calls):
    """Latency stats over runs that COMPLETED with reward 1 (a failed run's
    timings aren't comparable). Keyed by the unit each metric is defined on."""
    ok = {(r["workload"], r["run"]) for r in runs
          if r["status"] == "COMPLETED" and r["reward"] == 1.0 and not r["missing_timing"]}
    ok_runs = [r for r in runs if (r["workload"], r["run"]) in ok]
    ok_cycles = [c for c in cycles if (c["workload"], c["run"]) in ok]
    ok_calls = [c for c in calls if (c["workload"], c["run"]) in ok]
    fn_calls = [c for c in ok_calls if c["kind"] == "function"]
    tool_calls = [c for c in ok_calls if c["kind"] == "tool"]
    summary = {
        "runs_total": len(runs),
        "runs_used": len(ok_runs),
        "per_run": {
            "Te2e_ms": _stats([r["te2e_ms"] for r in ok_runs]),
            "T_LLM_ms": _stats([r["t_llm_ms"] for r in ok_runs]),
            "Torch_ms": _stats([r["torch_ms"] for r in ok_runs]),
            "Troute_ms": _stats([r["troute_ms"] for r in ok_runs]),
            "Rfriction": _stats([r["rfriction"] for r in ok_runs]),
        },
        "per_cycle": {
            "Tcycle_ms": _stats([c["tcycle_ms"] for c in ok_cycles]),
            "T_LLM_ms": _stats([c["t_llm_ms"] for c in ok_cycles]),
            "T_FaaS_HTTP_ms": _stats([c["t_faas_http_ms"] for c in ok_cycles]),
            "Torch_ms": _stats([c["torch_ms"] for c in ok_cycles]),
        },
        "per_tool_call": {
            "Twarm_ms": _stats([c["twarm_ms"] for c in tool_calls]),
            "Troute_ms": _stats([c["troute_ms"] for c in tool_calls]),
        },
        "per_function_call": {
            name: {
                "Twarm_ms": _stats([c["twarm_ms"] for c in fn_calls if c["name"] == name]),
                "Troute_ms": _stats([c["troute_ms"] for c in fn_calls if c["name"] == name]),
            }
            for name in ("planner", "actor", "evaluator")
        },
    }
    return summary


# ---------------------------------------------------------------- driver

def run_once(args, workload):
    gw = args.gateway
    _call(gw, workload["tool_function"], {"action": "reset"})
    wf_input = {"goal": workload["goal"], "domain": workload["domain"], "context": ""}

    t0 = time.time() * 1000.0  # [t0] dispatch
    wf_id = start_workflow(args.conductor, wf_input)
    t7, status = wait_for_workflow(args.conductor, wf_id, args.poll_interval, args.timeout)  # [t7]

    wf = get_workflow(args.conductor, wf_id)
    answer = (wf.get("output") or {}).get("final_answer") or ""
    result = score(workload, gw, answer if status == "COMPLETED" else "")
    run_row, cycle_rows, call_rows = compute_metrics(wf, t0, t7)
    run_row.update(workflow_id=wf_id, status=status, reward=result["reward"],
                   db_state_correct=result["db_state_correct"],
                   outputs_found=all(result["outputs_found"].values()))
    dp = dataplane.dataplane_metrics(dataplane_calls(wf), t0, t7)
    return wf, run_row, cycle_rows, call_rows, dp


def dataplane_calls(wf):
    """planner/actor/evaluator calls for dataplane.py; hop_ms = t5 - t2 from the
    HTTP task's start/end (Conductor clock)."""
    calls = []
    for t in wf.get("tasks", []):
        body = task_body(t)
        timing = body.get("_timing") if isinstance(body, dict) else None
        if t.get("taskType") != "HTTP" or timing is None:
            continue
        calls.append({
            "name": _ref(t).replace("_task", ""), "t3": timing["t3"], "t4": timing["t4"],
            "llm_ms": timing["llm_ms"], "tool_calls": timing.get("tool_calls", []),
            "hop_ms": t["endTime"] - t["startTime"],
        })
    return calls


def _write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()} for r in rows)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--conductor", required=True, help="Conductor API base, e.g. http://host:8080/api")
    p.add_argument("--gateway", required=True, help="faasd gateway, e.g. http://host:8080")
    p.add_argument("--workloads", nargs="+",
                   default=[os.path.join(REPO, "workloads", w) for w in ("retail-44.json", "airline-26.json")])
    p.add_argument("--runs", type=int, default=10, help="measured runs per workload")
    p.add_argument("--warmup", type=int, default=1, help="discarded runs per workload (keep tools warm)")
    p.add_argument("--poll-interval", type=float, default=0.05, help="seconds between status polls")
    p.add_argument("--pause", type=float, default=1.0, help="seconds to wait between runs")
    p.add_argument("--timeout", type=float, default=900, help="per-run timeout in seconds")
    p.add_argument("--out", default=os.path.join(REPO, "experiments", "results"))
    args = p.parse_args()
    args.conductor = args.conductor.rstrip("/")
    args.gateway = args.gateway.rstrip("/")

    out_dir = os.path.join(args.out, "exp1-conductor-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(out_dir, "raw"), exist_ok=True)

    runs, cycles, calls = [], [], []
    dp_runs, dp_turns, dp_calls = [], [], []
    for path in args.workloads:
        with open(path) as f:
            workload = json.load(f)
        for i in range(-args.warmup, args.runs):
            label = "warmup" if i < 0 else f"run {i + 1}/{args.runs}"
            wf, run_row, cycle_rows, call_rows, dp = run_once(args, workload)
            print(f"[{workload['id']}] {label}: {run_row['status']} reward={run_row['reward']} "
                  f"Te2e={run_row['te2e_ms']:.0f}ms cycles={run_row['n_cycles']} "
                  f"Torch={run_row['torch_ms']:.0f}ms Troute={run_row['troute_ms']:.0f}ms "
                  f"Rfriction={run_row['rfriction'] if run_row['rfriction'] is None else round(run_row['rfriction'], 4)}",
                  flush=True)
            if run_row["missing_timing"]:
                print("  WARNING: some HTTP tasks had no _timing in their response body - "
                      "are the instrumented functions deployed?", flush=True)
            if i >= 0:
                key = {"workload": workload["id"], "run": i + 1}
                runs.append({**key, **run_row})
                cycles += [{**key, **c} for c in cycle_rows]
                calls += [{**key, **c} for c in call_rows]
                dp_keep = {k: run_row[k] for k in ("status", "reward", "missing_timing")}
                dp_runs.append({**key, **dp[0], **dp_keep})
                dp_turns += [{**key, **t} for t in dp[1]]
                dp_calls += [{**key, **c} for c in dp[2]]
                with open(os.path.join(out_dir, "raw", f"{workload['id']}-run{i + 1}.json"), "w") as f:
                    json.dump(wf, f)
            time.sleep(args.pause)

    _write_csv(os.path.join(out_dir, "runs.csv"), runs)
    _write_csv(os.path.join(out_dir, "cycles.csv"), cycles)
    _write_csv(os.path.join(out_dir, "calls.csv"), calls)
    _write_csv(os.path.join(out_dir, "dataplane_runs.csv"), dp_runs)
    _write_csv(os.path.join(out_dir, "dataplane_turns.csv"), dp_turns)
    _write_csv(os.path.join(out_dir, "dataplane_calls.csv"), dp_calls)

    summary = {"config": {k: v for k, v in vars(args).items()}, "overall": summarize(runs, cycles, calls)}
    for wid in sorted({r["workload"] for r in runs}):
        pick = lambda rows: [r for r in rows if r["workload"] == wid]  # noqa: E731
        summary[wid] = summarize(pick(runs), pick(cycles), pick(calls))
    # Same definitions as exp1_argo.py - use these to compare the two stacks
    dp_ok = [r for r in dp_runs if r["status"] == "COMPLETED" and r["reward"] == 1.0 and not r["missing_timing"]]
    summary["stack"] = "conductor+faasd"
    summary["dataplane"] = {"overall": dataplane.summarize(dp_ok, dp_turns, dp_calls)}
    for wid in sorted({r["workload"] for r in dp_runs}):
        summary["dataplane"][wid] = dataplane.summarize(
            [r for r in dp_ok if r["workload"] == wid], dp_turns, dp_calls)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    o = summary["overall"]
    print(f"\nResults in {out_dir}  ({o['runs_used']}/{o['runs_total']} runs used)")
    rows = [
        ("Te2e (per run)", o["per_run"]["Te2e_ms"]),
        ("Tcycle (per turn)", o["per_cycle"]["Tcycle_ms"]),
        ("T_LLM (per turn)", o["per_cycle"]["T_LLM_ms"]),
        ("Torch (per turn)", o["per_cycle"]["Torch_ms"]),
        ("Twarm (per tool call)", o["per_tool_call"]["Twarm_ms"]),
        ("Troute (per tool call)", o["per_tool_call"]["Troute_ms"]),
        ("Rfriction (per run)", o["per_run"]["Rfriction"]),
    ]
    print("\nConductor + faasd (Conductor task timestamps)")
    print(f"{'metric':<24}{'n':>5}{'mean':>12}{'p50':>12}{'p95':>12}{'std':>12}")
    for name, s in rows:
        if s["n"]:
            print(f"{name:<24}{s['n']:>5}{s['mean']:>12}{s['p50']:>12}{s['p95']:>12}{s['std']:>12}")
    dataplane.print_table(summary["dataplane"]["overall"],
                          "Conductor + faasd (data-plane metrics, comparable with exp1_argo.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
