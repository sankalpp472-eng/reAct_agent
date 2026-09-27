"""
Experiment 1 (single-agent baseline, warm functions) for the Argo Workflows +
Knative stack. The counterpart of exp1_conductor.py.

Each run submits a Workflow from the `pae-agentic-loop` WorkflowTemplate
(argo-knative/argo-workflowtemplate.yaml) through the Kubernetes API, watches it
until it finishes, then computes the metrics from the functions' `_timing`
(experiments/dataplane.py - the same definitions used for Conductor + faasd).

  t0  driver clock, just before the Workflow object is created
  t7  driver clock, when the watch reports the Workflow finished
  everything else: the functions' own ms timestamps (see dataplane.py)

Argo records node start/finish only to the second; those are kept as an
engine-side sanity check (te2e_engine_s) but not used for the metrics.

Needs: pip install kubernetes   (uses your kubeconfig, like kubectl)

Usage:
  python experiments/exp1_argo.py \\
      --ingress knative://<node-ip>:80/default.example.com \\
      --runs 10 --warmup 1
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

from kubernetes import client, config, watch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "workloads"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import _call, score  # noqa: E402
from dataplane import dataplane_metrics, print_table, summarize  # noqa: E402

GROUP, VERSION, PLURAL = "argoproj.io", "v1alpha1", "workflows"
FUNCTIONS = ("planner", "actor", "evaluator")
DONE_PHASES = ("Succeeded", "Failed", "Error")


def run_workflow(api, namespace, template, wf_input, timeout_s):
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Workflow",
        "metadata": {"generateName": f"{template}-", "namespace": namespace},
        "spec": {
            "workflowTemplateRef": {"name": template},
            "arguments": {"parameters": [{"name": k, "value": v} for k, v in wf_input.items()]},
        },
    }
    t0 = time.time() * 1000.0  # [t0] dispatch
    created = api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, body)
    name = created["metadata"]["name"]

    # [t7]: watch from the creation's resourceVersion, so no update is missed
    phase = None
    w = watch.Watch()
    for event in w.stream(
        api.list_namespaced_custom_object, GROUP, VERSION, namespace, PLURAL,
        field_selector=f"metadata.name={name}",
        resource_version=created["metadata"]["resourceVersion"],
        timeout_seconds=int(timeout_s),
    ):
        phase = (event["object"].get("status") or {}).get("phase")
        if phase in DONE_PHASES:
            break
    t7 = time.time() * 1000.0
    w.stop()
    if phase not in DONE_PHASES:
        phase = "DRIVER_TIMEOUT"

    wf = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
    return wf, name, phase, t0, t7


def controller_requeue_time():
    """DEFAULT_REQUEUE_TIME on the workflow controller (Argo's default: 10s).
    It sets how quickly Argo moves between steps, so it's recorded with every run."""
    try:
        dep = client.AppsV1Api().read_namespaced_deployment("workflow-controller", "argo")
        for e in dep.spec.template.spec.containers[0].env or []:
            if e.name == "DEFAULT_REQUEUE_TIME":
                return e.value
    except client.ApiException:
        return "unknown"
    return "10s (default)"


def extract_calls(wf):
    """One record per planner/actor/evaluator HTTP node, from its response body."""
    calls, missing = [], 0
    for node in ((wf.get("status") or {}).get("nodes") or {}).values():
        if node.get("displayName") not in FUNCTIONS or node.get("type") != "HTTP":
            continue
        try:
            body = json.loads((node.get("outputs") or {}).get("result") or "")
        except ValueError:
            body = None
        timing = body.get("_timing") if isinstance(body, dict) else None
        if timing is None:
            missing += 1
            continue
        calls.append({
            "name": node["displayName"], "t3": timing["t3"], "t4": timing["t4"],
            "llm_ms": timing["llm_ms"], "tool_calls": timing.get("tool_calls", []),
            "hop_ms": None,  # Argo has no ms-precise per-node times; see dataplane.py
            "body": body,
        })
    return calls, missing


def _engine_seconds(wf):
    st = wf.get("status") or {}
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(st["finishedAt"], fmt) - datetime.strptime(st["startedAt"], fmt)).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def run_once(args, api, workload):
    _call(args.ingress, workload["tool_function"], {"action": "reset"})
    wf_input = {"goal": workload["goal"], "domain": workload["domain"], "context": ""}
    wf, name, phase, t0, t7 = run_workflow(api, args.namespace, args.template, wf_input, args.timeout)

    calls, missing = extract_calls(wf)
    evaluations = [c["body"] for c in sorted(calls, key=lambda c: c["t3"]) if c["name"] == "evaluator"]
    last = evaluations[-1] if evaluations else {}
    verdict = last.get("verdict")
    answer = last.get("feedback", "") if verdict == "done" else ""
    result = score(workload, args.ingress, answer)

    run_row, turn_rows, call_rows = dataplane_metrics(calls, t0, t7) if calls else ({}, [], [])
    run_row.update(
        workflow=name, phase=phase, verdict=verdict, reward=result["reward"],
        missing_timing=missing > 0, te2e_engine_s=_engine_seconds(wf),
    )
    if args.delete:
        api.delete_namespaced_custom_object(GROUP, VERSION, args.namespace, PLURAL, name)
    return wf, run_row, turn_rows, call_rows


def write_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()} for r in rows)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ingress", required=True,
                   help="Knative ingress for reset/score, e.g. knative://<node-ip>:80/default.example.com")
    p.add_argument("--namespace", default="argo")
    p.add_argument("--template", default="pae-agentic-loop")
    p.add_argument("--workloads", nargs="+",
                   default=[os.path.join(REPO, "workloads", w) for w in ("retail-44.json", "airline-26.json")])
    p.add_argument("--runs", type=int, default=10, help="measured runs per workload")
    p.add_argument("--warmup", type=int, default=1, help="discarded runs per workload")
    p.add_argument("--pause", type=float, default=1.0, help="seconds to wait between runs")
    p.add_argument("--timeout", type=float, default=900, help="per-run timeout in seconds")
    p.add_argument("--delete", action="store_true", help="delete each Workflow after reading it")
    p.add_argument("--out", default=os.path.join(REPO, "experiments", "results"))
    args = p.parse_args()

    config.load_kube_config()
    api = client.CustomObjectsApi()
    requeue = controller_requeue_time()
    print(f"Argo controller DEFAULT_REQUEUE_TIME: {requeue}")

    out_dir = os.path.join(args.out, "exp1-argo-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(out_dir, "raw"), exist_ok=True)

    runs, turns, calls = [], [], []
    for path in args.workloads:
        with open(path) as f:
            workload = json.load(f)
        for i in range(-args.warmup, args.runs):
            label = "warmup" if i < 0 else f"run {i + 1}/{args.runs}"
            wf, run_row, turn_rows, call_rows = run_once(args, api, workload)
            rf = run_row.get("rfriction")
            print(f"[{workload['id']}] {label}: {run_row['phase']} verdict={run_row['verdict']} "
                  f"reward={run_row['reward']} Te2e={run_row.get('te2e_ms', 0):.0f}ms "
                  f"turns={run_row.get('n_turns')} Torch_run={run_row.get('torch_run_ms', 0):.0f}ms "
                  f"Rfriction={None if rf is None else round(rf, 4)}", flush=True)
            if run_row["missing_timing"]:
                print("  WARNING: some HTTP nodes had no _timing - are the instrumented images deployed?",
                      flush=True)
            if i >= 0:
                key = {"workload": workload["id"], "run": i + 1}
                runs.append({**key, **run_row})
                turns += [{**key, **t} for t in turn_rows]
                calls += [{**key, **c} for c in call_rows]
                with open(os.path.join(out_dir, "raw", f"{workload['id']}-run{i + 1}.json"), "w") as f:
                    json.dump(wf, f)
            time.sleep(args.pause)

    write_csv(os.path.join(out_dir, "dataplane_runs.csv"), runs)
    write_csv(os.path.join(out_dir, "dataplane_turns.csv"), turns)
    write_csv(os.path.join(out_dir, "dataplane_calls.csv"), calls)

    ok = [r for r in runs if r["phase"] == "Succeeded" and r["reward"] == 1.0 and not r["missing_timing"]]
    summary = {"config": vars(args), "stack": "argo+knative", "argo_requeue_time": requeue,
               "runs_total": len(runs),
               "dataplane": {"overall": summarize(ok, turns, calls)}}
    for wid in sorted({r["workload"] for r in runs}):
        summary["dataplane"][wid] = summarize([r for r in ok if r["workload"] == wid], turns, calls)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nResults in {out_dir}  ({len(ok)}/{len(runs)} runs used)")
    print_table(summary["dataplane"]["overall"], "Argo + Knative (data-plane metrics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
