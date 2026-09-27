"""
Sworkflow (logical): how much state the orchestrator keeps for one workflow
run, computed from the execution records the Exp 1 drivers save in raw/.

  Conductor + faasd   raw/*.json is GET /api/workflow/{id}?includeTasks=true:
                      the execution Conductor persists, with all its tasks
  Argo + Knative      raw/*.json is the Workflow object from the Kubernetes API:
                      what the API server keeps in its datastore (etcd, or
                      SQLite on k3s by default)

Size = the record re-serialized as compact JSON (no whitespace), so both stacks
are measured the same way. It's the *logical* size of one run's state, not the
physical growth of the database (indexes, Elasticsearch copies, Redis overhead,
etcd's revision history of every status update all add to that).

Each record is also split into:
  payload      data passed between steps: workflow input/output/variables and
               every task's (Conductor) / node's (Argo) inputs and outputs
  definitions  copies of the workflow/task definitions stored with the run
               (Conductor: workflowDefinition + each task's workflowTask and
               taskDefinition; Argo: spec + storedTemplates +
               storedWorkflowTemplateSpec)
  other        everything else: ids, timestamps, statuses, Kubernetes metadata

Usage:
  python experiments/sworkflow.py experiments/results/exp1-conductor-<ts> [experiments/results/exp1-argo-<ts> ...]
Writes sworkflow_runs.csv into each folder, adds "sworkflow" to its
summary.json, and prints a table per folder.
"""
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataplane import stats  # noqa: E402


def _size(obj):
    return len(json.dumps(obj, separators=(",", ":"))) if obj is not None else 0


def measure_conductor(wf):
    tasks = wf.get("tasks", [])
    definitions = _size(wf.get("workflowDefinition")) + sum(
        _size(t.get("workflowTask")) + _size(t.get("taskDefinition")) for t in tasks)
    payload = _size(wf.get("input")) + _size(wf.get("output")) + _size(wf.get("variables")) + sum(
        _size(t.get("inputData")) + _size(t.get("outputData")) for t in tasks)
    turns = sum(1 for t in tasks if t.get("referenceTaskName", "").split("__")[0] == "planner_task")
    return {"stack": "conductor+faasd", "definitions_bytes": definitions, "payload_bytes": payload,
            "turns": turns, "units": len(tasks)}  # units = tasks


def measure_argo(wf):
    st = wf.get("status") or {}
    nodes = (st.get("nodes") or {}).values()
    definitions = _size(wf.get("spec")) + _size(st.get("storedTemplates")) + \
        _size(st.get("storedWorkflowTemplateSpec"))
    payload = sum(_size(n.get("inputs")) + _size(n.get("outputs")) for n in nodes)
    turns = sum(1 for n in nodes if n.get("type") == "HTTP" and n.get("displayName") == "planner")
    return {"stack": "argo+knative", "definitions_bytes": definitions, "payload_bytes": payload,
            "turns": turns, "units": len(nodes)}  # units = nodes


def measure(wf):
    row = measure_argo(wf) if "apiVersion" in wf else measure_conductor(wf)
    total = _size(wf)
    row["total_bytes"] = total
    row["other_bytes"] = total - row["definitions_bytes"] - row["payload_bytes"]
    row["kb_per_turn"] = total / 1024 / row["turns"] if row["turns"] else None
    return row


def _ok_runs(folder):
    """(workload, run) of runs that completed with reward 1, if the folder
    records it; None = unknown (then all runs are used)."""
    for name, status_key, good in (("runs.csv", "status", "COMPLETED"),
                                   ("dataplane_runs.csv", "phase", "Succeeded")):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path) as f:
                rows = list(csv.DictReader(f))
            if rows and status_key in rows[0]:
                return {(r["workload"], int(r["run"])) for r in rows
                        if r.get(status_key) == good and float(r.get("reward") or 0) == 1.0}
    return None


def process(folder):
    raw = os.path.join(folder, "raw")
    ok = _ok_runs(folder)
    rows = []
    for fname in sorted(os.listdir(raw)):
        m = re.match(r"(.+)-run(\d+)\.json$", fname)
        if not m:
            continue  # e.g. exp2b's trial files
        workload, run = m.group(1), int(m.group(2))
        with open(os.path.join(raw, fname)) as f:
            row = measure(json.load(f))
        rows.append({"workload": workload, "run": run,
                     "used": ok is None or (workload, run) in ok, **row})
    if not rows:
        print(f"{folder}: no raw/*-run*.json files")
        return

    with open(os.path.join(folder, "sworkflow_runs.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()} for r in rows)

    def block(rs):
        kb = lambda key: stats([r[key] / 1024 for r in rs])  # noqa: E731
        return {"runs": len(rs), "total_kb": kb("total_bytes"), "kb_per_turn": stats([r["kb_per_turn"] for r in rs]),
                "payload_kb": kb("payload_bytes"), "definitions_kb": kb("definitions_bytes"),
                "other_kb": kb("other_bytes")}
    used = [r for r in rows if r["used"]]
    section = {"stack": rows[0]["stack"], "unit": "KB = 1024 bytes of compact JSON", "overall": block(used)}
    for wid in sorted({r["workload"] for r in used}):
        section[wid] = block([r for r in used if r["workload"] == wid])

    spath = os.path.join(folder, "summary.json")
    summary = json.load(open(spath)) if os.path.exists(spath) else {}
    summary["sworkflow"] = section
    with open(spath, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{folder}  ({section['stack']}, {len(used)}/{len(rows)} runs used)")
    print(f"{'':<22}{'total KB':>10}{'KB/turn':>10}{'payload':>10}{'defs':>10}{'other':>10}")
    for key in [k for k in section if k not in ("stack", "unit", "overall")] + ["overall"]:
        b = section[key]
        if b["runs"]:
            print(f"{key:<22}{b['total_kb']['mean']:>10.1f}{b['kb_per_turn']['mean']:>10.1f}"
                  f"{b['payload_kb']['mean']:>10.1f}{b['definitions_kb']['mean']:>10.1f}{b['other_kb']['mean']:>10.1f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for folder in sys.argv[1:]:
        process(folder.rstrip("/"))
