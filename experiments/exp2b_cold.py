"""
Experiment 2b: what a cold tool function costs the agent, measured through the
orchestrator.

Each trial is a pair of runs of the same workload through the real workflow
(Conductor + faasd, or Argo + Knative):

  1. make the workload's tool function (retail-tools / airline-tools) dormant
       faasd    stop its process (containerd task). faasd reports 0 replicas,
                and the gateway (scale_from_zero=true) starts a fresh process
                on the next request - faasd CE can't be scaled to 0 any
                other way ("replicas must > 0 for faasd CE").
       Knative  let it scale to zero (min-scale 0 during this experiment) and
                wait until its pod is gone; the next request goes through
                the activator, which waits for a new pod.
  2. COLD run: run the workflow. No DB reset beforehand - that would wake the
     function; a new process starts with a fresh DB anyway.
  3. WARM run: reset the DB (the function is warm now) and run the workflow
     again, as the paired baseline.

Per trial it reports, from the same timestamps as Exp 1 (dataplane.py):
  delta_te2e_ms       Te2e(cold) - Te2e(warm): the end-to-end cost of the cold tool
  tcold_tool_ms       first tool call's round trip in the cold run (actor clock,
                      t5 - t2) minus the median warm tool-call round trip
  cold_handler_ms     that first call's handler time (t4 - t3); includes the tool
                      function's lazy DB load, i.e. app-level init
  first_call_error    whether the cold call failed instead (e.g. timed out)
Only the tool function is made cold; planner/actor/evaluator stay warm.

Usage (driver on the same host as the stack):
  # Conductor + faasd - needs sudo for ctr
  python experiments/exp2b_cold.py --stack conductor \\
      --conductor http://localhost:8082/api --gateway http://127.0.0.1:8080 --trials 5

  # Argo + Knative
  python experiments/exp2b_cold.py --stack argo \\
      --ingress knative://<kourier-ip>:80/default.example.com --trials 5
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "workloads"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import _call, score  # noqa: E402
import dataplane  # noqa: E402


# ------------------------------------------------------------ stack adapters

class ConductorFaasd:
    """Runs workflows through Conductor; makes faasd functions dormant by
    stopping their containerd task."""

    def __init__(self, args):
        import exp1_conductor as c
        self.c, self.args = c, args
        self.tool_gw = args.gateway

    def describe(self):
        return {"stack": "conductor+faasd", "cold_mechanism": "containerd task stopped; gateway scale_from_zero"}

    def _task_status(self, fn):
        out = subprocess.run(self.args.ctr.split() + ["-n", "openfaas-fn", "task", "ls"],
                             capture_output=True, text=True, check=True).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if parts and parts[0] == fn:
                return parts[-1].upper()
        return "ABSENT"

    def make_dormant(self, fn):
        subprocess.run(self.args.ctr.split() + ["-n", "openfaas-fn", "task", "kill", "-s", "SIGKILL", fn],
                       capture_output=True, text=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._task_status(fn) in ("STOPPED", "ABSENT"):
                return
            time.sleep(0.2)
        raise RuntimeError(f"{fn} did not stop (task status {self._task_status(fn)})")

    def run(self, workload):
        wf_input = {"goal": workload["goal"], "domain": workload["domain"], "context": ""}
        t0 = time.time() * 1000.0
        wf_id = self.c.start_workflow(self.args.conductor, wf_input)
        t7, status = self.c.wait_for_workflow(self.args.conductor, wf_id, 0.05, self.args.timeout)
        wf = self.c.get_workflow(self.args.conductor, wf_id)
        answer = (wf.get("output") or {}).get("final_answer") or "" if status == "COMPLETED" else ""
        return wf, status, status == "COMPLETED", answer, self.c.dataplane_calls(wf), t0, t7

    def finish(self):
        pass


class ArgoKnative:
    """Runs workflows through Argo; makes Knative services dormant by letting
    them scale to zero."""

    def __init__(self, args):
        from kubernetes import client, config
        import exp1_argo as a
        config.load_kube_config()
        self.a, self.args = a, args
        self.api = client.CustomObjectsApi()
        self.core = client.CoreV1Api()
        self.tool_gw = args.ingress
        self.patched = set()

    def describe(self):
        return {"stack": "argo+knative", "cold_mechanism": "Knative scale to zero; activator",
                "argo_requeue_time": self.a.controller_requeue_time()}

    def _set_min_scale(self, fn, value):
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "autoscaling.knative.dev/min-scale": value}}}}}
        self.api.patch_namespaced_custom_object(
            "serving.knative.dev", "v1", "default", "services", fn, patch)

    def _wait_revision_ready(self, fn, timeout=180):
        """Changing min-scale creates a new revision. Make sure it becomes
        Ready (otherwise traffic stays on the old, pinned revision)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.api.get_namespaced_custom_object(
                "serving.knative.dev", "v1", "default", "services", fn).get("status", {})
            created, ready = st.get("latestCreatedRevisionName"), st.get("latestReadyRevisionName")
            if created and created == ready:
                return
            rev = self.api.get_namespaced_custom_object(
                "serving.knative.dev", "v1", "default", "revisions", created) if created else {}
            cond = next((c for c in rev.get("status", {}).get("conditions", []) if c.get("type") == "Ready"), {})
            if cond.get("status") == "False":
                raise RuntimeError(f"new revision {created} of {fn} failed: {cond.get('reason')}: "
                                   f"{cond.get('message')}")
            time.sleep(2)
        raise RuntimeError(f"new revision of {fn} not Ready within {timeout}s")

    def _pods(self, fn):
        """Pods of this service that could still serve: finished pods left
        behind (Completed/Error, e.g. after a k3s restart) don't count."""
        pods = self.core.list_namespaced_pod(
            "default", label_selector=f"serving.knative.dev/service={fn}").items
        return [f"{p.metadata.name} ({p.status.phase}{', terminating' if p.metadata.deletion_timestamp else ''})"
                for p in pods if p.status.phase not in ("Succeeded", "Failed")]

    def make_dormant(self, fn):
        if fn not in self.patched:
            self.patched.add(fn)
            self._set_min_scale(fn, "0")  # new revision that may scale to zero
            self._wait_revision_ready(fn)
        print(f"  waiting for {fn} to scale to zero ...", flush=True)
        start = time.time()
        next_report = start + 60
        while time.time() - start < self.args.scale_down_timeout:
            pods = self._pods(fn)
            if not pods:
                print(f"  {fn} at zero after {time.time() - start:.0f}s", flush=True)
                time.sleep(2)  # let Knative's routing settle on the zero state
                return
            if time.time() >= next_report:
                print(f"  still waiting ({time.time() - start:.0f}s): {', '.join(pods)}", flush=True)
                next_report += 60
            time.sleep(2)
        raise RuntimeError(
            f"{fn} did not scale to zero within {self.args.scale_down_timeout}s; still there: "
            f"{', '.join(self._pods(fn))}. Check: kubectl get revision,podautoscaler,pods -n default "
            f"| grep {fn}")

    def run(self, workload):
        wf_input = {"goal": workload["goal"], "domain": workload["domain"], "context": ""}
        wf, name, phase, t0, t7 = self.a.run_workflow(
            self.api, "argo", "pae-agentic-loop", wf_input, self.args.timeout)
        calls, _ = self.a.extract_calls(wf)
        evals = [c["body"] for c in sorted(calls, key=lambda c: c["t3"]) if c["name"] == "evaluator"]
        last = evals[-1] if evals else {}
        ok = phase == "Succeeded" and last.get("verdict") == "done"
        return wf, phase, ok, last.get("feedback", "") if ok else "", calls, t0, t7

    def finish(self):
        for fn in self.patched:  # back to Exp 1's always-warm setting
            self._set_min_scale(fn, "1")
            try:
                self._wait_revision_ready(fn)
                print(f"restored min-scale 1 on {fn}")
            except RuntimeError as e:
                print(f"WARNING: restoring min-scale 1 on {fn}: {e}")


# ------------------------------------------------------------ one trial

def tool_calls(calls):
    return sorted((tc for c in calls for tc in c.get("tool_calls", [])), key=lambda tc: tc["t2"])


def warm_tool_http(calls):
    return [tc["http_ms"] for c in calls for tc in c.get("tool_calls", []) if "twarm_ms" in tc]


def planner_turns(calls):
    return sum(1 for c in calls if c["name"] == "planner")


def _ms(v):
    return "n/a" if v is None else f"{v:.0f}ms"


def trial(stack, workload):
    fn = workload["tool_function"]
    stack.make_dormant(fn)

    # COLD run - no reset (it would wake the function; a new process has a fresh DB)
    wf_c, st_c, ok_c, ans_c, calls_c, t0c, t7c = stack.run(workload)
    reward_c = score(workload, stack.tool_gw, ans_c)["reward"]

    # WARM run - paired baseline
    _call(stack.tool_gw, fn, {"action": "reset"})
    wf_w, st_w, ok_w, ans_w, calls_w, t0w, t7w = stack.run(workload)
    reward_w = score(workload, stack.tool_gw, ans_w)["reward"]

    tcs = tool_calls(calls_c)
    first = tcs[0] if tcs else None
    first_ok = next((tc for tc in tcs if "twarm_ms" in tc), None)
    failed = bool(first) and "twarm_ms" not in first
    warm_med = statistics.median(warm_tool_http(calls_w)) if warm_tool_http(calls_w) else None
    row = {
        "status_cold": st_c, "reward_cold": reward_c,
        "status_warm": st_w, "reward_warm": reward_w,
        "te2e_cold_ms": t7c - t0c, "te2e_warm_ms": t7w - t0w,
        "delta_te2e_ms": (t7c - t0c) - (t7w - t0w),
        "turns_cold": planner_turns(calls_c), "turns_warm": planner_turns(calls_w),
        "first_tool": first["tool"] if first else None,
        "first_call_error": failed,
        # recorded by actor images built with the error-recording change
        "first_call_error_detail": first.get("error") if failed else None,
        "failed_tool_calls_cold": sum(1 for tc in tcs if "twarm_ms" not in tc),
        "cold_first_call_http_ms": first["http_ms"] if first else None,
        "cold_handler_ms": first.get("twarm_ms") if first and not failed else None,
        "warm_tool_http_median_ms": warm_med,
        # the cold start as the agent sees it - only defined if the cold call succeeded
        "tcold_tool_ms": (first["http_ms"] - warm_med) if first and not failed and warm_med is not None else None,
        # first attempt sent -> first successful tool answer back; with a failed
        # first call this includes the agent's retry (re-plan turn)
        "time_to_first_tool_ok_ms": (first_ok["t5"] - first["t2"]) if first and first_ok else None,
    }
    dp_c = dataplane.dataplane_metrics(calls_c, t0c, t7c)[0] if calls_c else {}
    dp_w = dataplane.dataplane_metrics(calls_w, t0w, t7w)[0] if calls_w else {}
    row["torch_run_cold_ms"] = dp_c.get("torch_run_ms")
    row["torch_run_warm_ms"] = dp_w.get("torch_run_ms")
    return row, {"cold": wf_c, "warm": wf_w}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stack", required=True, choices=["conductor", "argo"])
    p.add_argument("--conductor", help="Conductor API base (conductor stack)")
    p.add_argument("--gateway", help="faasd gateway (conductor stack)")
    p.add_argument("--ctr", default="sudo ctr", help="command to run containerd's ctr (conductor stack)")
    p.add_argument("--ingress", help="Knative ingress, knative://<ip>:80/default.example.com (argo stack)")
    p.add_argument("--scale-down-timeout", type=float, default=600,
                   help="seconds to wait for a Knative service to reach zero pods")
    p.add_argument("--workloads", nargs="+",
                   default=[os.path.join(REPO, "workloads", w) for w in ("retail-44.json", "airline-26.json")])
    p.add_argument("--trials", type=int, default=5, help="cold+warm pairs per workload")
    p.add_argument("--timeout", type=float, default=900, help="per-run timeout in seconds")
    p.add_argument("--out", default=os.path.join(REPO, "experiments", "results"))
    args = p.parse_args()
    if args.stack == "conductor" and not (args.conductor and args.gateway):
        p.error("--stack conductor needs --conductor and --gateway")
    if args.stack == "argo" and not args.ingress:
        p.error("--stack argo needs --ingress")
    if args.conductor:
        args.conductor = args.conductor.rstrip("/")
    if args.gateway:
        args.gateway = args.gateway.rstrip("/")

    stack = ConductorFaasd(args) if args.stack == "conductor" else ArgoKnative(args)
    info = stack.describe()
    print(json.dumps(info))

    out_dir = os.path.join(args.out, f"exp2b-{args.stack}-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(out_dir, "raw"), exist_ok=True)

    rows = []
    try:
        for path in args.workloads:
            with open(path) as f:
                workload = json.load(f)
            # one warm run first, so planner/actor/evaluator (and the actor's
            # cached tool list) are warm and only the tool function goes cold
            _call(stack.tool_gw, workload["tool_function"], {"action": "reset"})
            stack.run(workload)
            for i in range(args.trials):
                row, raw = trial(stack, workload)
                row = {"workload": workload["id"], "trial": i + 1, **row}
                rows.append(row)
                with open(os.path.join(out_dir, "raw", f"{workload['id']}-trial{i + 1}.json"), "w") as f:
                    json.dump(raw, f)
                if row["first_call_error"]:
                    first = (f"first tool call FAILED after {row['cold_first_call_http_ms']:.0f}ms "
                             f"({' '.join((row['first_call_error_detail'] or 'error not recorded: rebuild the actor').split())}), "
                             f"first tool answer after {_ms(row['time_to_first_tool_ok_ms'])}")
                else:
                    first = f"Tcold_tool={_ms(row['tcold_tool_ms'])}"
                print(f"[{workload['id']}] trial {i + 1}/{args.trials}: "
                      f"cold {row['status_cold']} r={row['reward_cold']} Te2e={row['te2e_cold_ms']:.0f}ms "
                      f"turns={row['turns_cold']} | "
                      f"warm {row['status_warm']} r={row['reward_warm']} Te2e={row['te2e_warm_ms']:.0f}ms "
                      f"turns={row['turns_warm']} | dTe2e={row['delta_te2e_ms']:.0f}ms | {first}", flush=True)
    finally:
        stack.finish()

    from exp1_conductor import _write_csv
    _write_csv(os.path.join(out_dir, "trials.csv"), rows)

    ok = [r for r in rows if r["reward_cold"] == 1.0 and r["reward_warm"] == 1.0]
    def block(rs):
        return {
            "trials": len(rs),
            "first_call_failed": sum(1 for r in rs if r["first_call_error"]),
            "extra_turns": dataplane.stats([r["turns_cold"] - r["turns_warm"] for r in rs]),
            "time_to_first_tool_ok_ms": dataplane.stats([r["time_to_first_tool_ok_ms"] for r in rs]),
            "delta_te2e_ms": dataplane.stats([r["delta_te2e_ms"] for r in rs]),
            "tcold_tool_ms": dataplane.stats([r["tcold_tool_ms"] for r in rs]),
            "cold_first_call_http_ms": dataplane.stats([r["cold_first_call_http_ms"] for r in rs]),
            "cold_handler_ms": dataplane.stats([r["cold_handler_ms"] for r in rs]),
            "warm_tool_http_median_ms": dataplane.stats([r["warm_tool_http_median_ms"] for r in rs]),
        }
    summary = {**info, "config": vars(args), "trials_total": len(rows), "trials_used": len(ok),
               "overall": block(ok)}
    for wid in sorted({r["workload"] for r in rows}):
        summary[wid] = block([r for r in ok if r["workload"] == wid])
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    o = summary["overall"]
    print(f"\nResults in {out_dir}  ({len(ok)}/{len(rows)} trials used: both runs reward 1; "
          f"first tool call failed in {o['first_call_failed']})")
    print(f"{'metric':<28}{'n':>4}{'mean':>12}{'p50':>12}{'p95':>12}{'std':>12}")
    for name in ("delta_te2e_ms", "extra_turns", "time_to_first_tool_ok_ms", "tcold_tool_ms",
                 "cold_first_call_http_ms", "cold_handler_ms", "warm_tool_http_median_ms"):
        s = o[name]
        if s.get("n"):
            print(f"{name:<28}{s['n']:>4}{s['mean']:>12}{s['p50']:>12}{s['p95']:>12}{s['std']:>12}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
