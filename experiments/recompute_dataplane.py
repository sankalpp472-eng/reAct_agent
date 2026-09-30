"""
Recompute the stack-independent data-plane metrics (dataplane.py) for an
existing results folder from its raw/*.json executions - no need to re-run
the experiment.

  python experiments/recompute_dataplane.py experiments/results/exp1-conductor-<timestamp>
  python experiments/recompute_dataplane.py experiments/results/exp1-argo-<timestamp>

Conductor folders: Te2e comes from runs.csv (the driver's t0/t7).
Argo folders (e.g. a run that was stopped before the driver wrote its CSVs):
Te2e comes from the Workflow's own timestamps, creationTimestamp -> finishedAt,
which Kubernetes records to the second (so +-1 s). The DB state can't be
re-checked afterwards, so reward is inferred: 1 if the loop ended with
verdict "done" and the final answer contains the workload's expected outputs
(reward_verified = False marks this).

Writes dataplane_runs.csv / dataplane_turns.csv / dataplane_calls.csv into that
folder, adds a "dataplane" section to its summary.json and prints the table.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dataplane  # noqa: E402
from exp1_conductor import _write_csv, dataplane_calls  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUNCTIONS = ("planner", "actor", "evaluator")


def _argo_calls(wf):
    calls = []
    for node in ((wf.get("status") or {}).get("nodes") or {}).values():
        if node.get("displayName") not in FUNCTIONS or node.get("type") != "HTTP":
            continue
        try:
            body = json.loads((node.get("outputs") or {}).get("result") or "")
        except ValueError:
            continue
        timing = body.get("_timing") if isinstance(body, dict) else None
        if timing:
            calls.append({"name": node["displayName"], "t3": timing["t3"], "t4": timing["t4"],
                          "llm_ms": timing["llm_ms"], "llm_wait_ms": timing.get("llm_wait_ms", 0.0),
                          "tool_calls": timing.get("tool_calls", []),
                          "hop_ms": None, "body": body})
    return calls


def _ts(s):
    from datetime import datetime, timezone
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000.0


def main_argo(folder):
    import re
    dp_runs, dp_turns, dp_calls = [], [], []
    for fname in sorted(os.listdir(os.path.join(folder, "raw"))):
        m = re.match(r"(.+)-run(\d+)\.json$", fname)
        if not m:
            continue
        with open(os.path.join(folder, "raw", fname)) as f:
            wf = json.load(f)
        with open(os.path.join(REPO, "workloads", f"{m.group(1)}.json")) as f:
            workload = json.load(f)
        key = {"workload": m.group(1), "run": int(m.group(2))}
        calls = _argo_calls(wf)
        st = wf.get("status") or {}
        t0, t7 = _ts(wf["metadata"]["creationTimestamp"]), _ts(st["finishedAt"])
        evals = [c["body"] for c in sorted(calls, key=lambda c: c["t3"]) if c["name"] == "evaluator"]
        last = evals[-1] if evals else {}
        answer = (last.get("feedback") or "").lower().replace(",", "")
        done = st.get("phase") == "Succeeded" and last.get("verdict") == "done"
        reward = 1.0 if done and all(o.lower() in answer for o in workload["expected_outputs"]) else 0.0
        run_row, turn_rows, call_rows = dataplane.dataplane_metrics(calls, t0, t7)
        run_row.update(workflow=wf["metadata"]["name"], phase=st.get("phase"), verdict=last.get("verdict"),
                       reward=reward, reward_verified=False, missing_timing=False,
                       te2e_source="k8s timestamps (1 s resolution)")
        dp_runs.append({**key, **run_row})
        dp_turns += [{**key, **t} for t in turn_rows]
        dp_calls += [{**key, **c} for c in call_rows]
    return dp_runs, dp_turns, dp_calls, "argo+knative", ("phase", "Succeeded")


def main(folder):
    if not os.path.exists(os.path.join(folder, "runs.csv")):
        dp_runs, dp_turns, dp_calls, stack, (skey, sval) = main_argo(folder)
    else:
        dp_runs, dp_turns, dp_calls = main_conductor(folder)
        stack, skey, sval = "conductor+faasd", "status", "COMPLETED"
    finish(folder, dp_runs, dp_turns, dp_calls, stack, skey, sval)


def main_conductor(folder):
    with open(os.path.join(folder, "runs.csv")) as f:
        runs = list(csv.DictReader(f))

    dp_runs, dp_turns, dp_calls = [], [], []
    for r in runs:
        with open(os.path.join(folder, "raw", f"{r['workload']}-run{r['run']}.json")) as f:
            wf = json.load(f)
        key = {"workload": r["workload"], "run": int(r["run"])}
        # only Te2e = t7 - t0 is used, so t0 = 0 is fine
        run_row, turn_rows, call_rows = dataplane.dataplane_metrics(
            dataplane_calls(wf), 0.0, float(r["te2e_ms"]))
        run_row.update(status=r["status"], reward=float(r["reward"]),
                       missing_timing=r["missing_timing"] == "True")
        dp_runs.append({**key, **run_row})
        dp_turns += [{**key, **t} for t in turn_rows]
        dp_calls += [{**key, **c} for c in call_rows]
    return dp_runs, dp_turns, dp_calls


def finish(folder, dp_runs, dp_turns, dp_calls, stack, skey, sval):
    _write_csv(os.path.join(folder, "dataplane_runs.csv"), dp_runs)
    _write_csv(os.path.join(folder, "dataplane_turns.csv"), dp_turns)
    _write_csv(os.path.join(folder, "dataplane_calls.csv"), dp_calls)

    ok = [r for r in dp_runs if r[skey] == sval and r["reward"] == 1.0 and not r["missing_timing"]]
    section = {"overall": dataplane.summarize(ok, dp_turns, dp_calls)}
    for wid in sorted({r["workload"] for r in dp_runs}):
        section[wid] = dataplane.summarize([r for r in ok if r["workload"] == wid], dp_turns, dp_calls)

    path = os.path.join(folder, "summary.json")
    summary = {}
    if os.path.exists(path):
        with open(path) as f:
            summary = json.load(f)
    summary["stack"] = stack
    if stack == "argo+knative":
        summary.setdefault("runs_total", len(dp_runs))
        summary["recomputed_from_raw"] = ("Te2e from Workflow creationTimestamp -> finishedAt (1 s "
                                          "resolution); reward inferred from verdict + answer, DB not re-checked")
    summary["dataplane"] = section
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"{len(ok)}/{len(dp_runs)} runs used; updated {folder}")
    dataplane.print_table(section["overall"], f"{stack} (data-plane metrics)")


if __name__ == "__main__":
    main(sys.argv[1])
