"""
Recompute the stack-independent data-plane metrics (dataplane.py) for an
existing Conductor + faasd results folder, from its raw/*.json executions and
runs.csv (for each run's Te2e) - no need to re-run the experiment.

  python experiments/recompute_dataplane.py experiments/results/exp1-conductor-<timestamp>

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


def main(folder):
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

    _write_csv(os.path.join(folder, "dataplane_runs.csv"), dp_runs)
    _write_csv(os.path.join(folder, "dataplane_turns.csv"), dp_turns)
    _write_csv(os.path.join(folder, "dataplane_calls.csv"), dp_calls)

    ok = [r for r in dp_runs if r["status"] == "COMPLETED" and r["reward"] == 1.0 and not r["missing_timing"]]
    section = {"overall": dataplane.summarize(ok, dp_turns, dp_calls)}
    for wid in sorted({r["workload"] for r in dp_runs}):
        section[wid] = dataplane.summarize([r for r in ok if r["workload"] == wid], dp_turns, dp_calls)

    path = os.path.join(folder, "summary.json")
    with open(path) as f:
        summary = json.load(f)
    summary["stack"] = "conductor+faasd"
    summary["dataplane"] = section
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"{len(ok)}/{len(dp_runs)} runs used; updated {folder}")
    dataplane.print_table(section["overall"], "Conductor + faasd (data-plane metrics)")


if __name__ == "__main__":
    main(sys.argv[1])
