"""
Run a workload through the planner -> actor -> evaluator loop and score it,
without Conductor. The loop mirrors conductor-agentic-loop/pae_agentic_loop_v2.json:
each iteration re-plans with the completed history, runs the actor on plan[0],
appends the result, and asks the evaluator; it stops on "done" or after 8 steps.

Two modes:
  # everything in-process: the five function directories are loaded and served
  # on a local stand-in gateway (the actor still reaches the tool servers over HTTP)
  python workloads/run_local.py workloads/retail-44.json

  # against a real faasd gateway where all five functions are deployed
  python workloads/run_local.py workloads/retail-44.json --gateway http://<faasd-host>:8080

Local mode needs `requests` (the actor's dependency). Set MOCK_LATENCY_S=0 for a
fast run; by default the mock sleeps like it does on faasd.
"""
import argparse
import importlib
import json
import os
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from score import _call, score

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUNCTIONS = ["planner", "actor", "evaluator", "retail-tools", "airline-tools"]
MAX_STEPS = 8  # same safety cap as the Conductor workflow's loopCondition
COMPLETED_MARKER = "Steps already completed so far (do not re-plan these, only plan what still remains): "


def _load_function(name):
    """Import <repo>/<name>/handler.py as a package, the way the python3-http
    template does (so its relative imports work)."""
    # The function dirs have no __init__.py (the template adds one at build
    # time), so register an empty package pointing at the directory.
    pkg = "fn_" + name.replace("-", "_")
    module = types.ModuleType(pkg)
    module.__path__ = [os.path.join(REPO, name)]
    sys.modules[pkg] = module
    return importlib.import_module(f"{pkg}.handler")


def _start_local_gateway():
    """Serve every function at /function/<name> on 127.0.0.1:<random port>."""
    handlers = {}

    class Event:
        def __init__(self, body, method):
            self.body, self.method = body, method

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            name = self.path.split("/function/", 1)[-1].strip("/")
            if name not in handlers:
                self.send_response(404)
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            resp = handlers[name].handle(Event(body, self.command), None)
            out = json.dumps(resp.get("body", "")).encode("utf-8")
            self.send_response(resp.get("statusCode", 200))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _serve

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    gateway = f"http://127.0.0.1:{server.server_port}"
    # the actor's tools.py reads this at import time, so set it before loading
    os.environ["TOOLS_GATEWAY_URL"] = gateway
    handlers.update({name: _load_function(name) for name in FUNCTIONS})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return gateway


def _post(gateway, function, body):
    req = urllib.request.Request(
        f"{gateway}/function/{function}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{function} returned {e.code}: {e.read().decode()[:500]}")


def run(workload, gateway):
    goal, domain = workload["goal"], workload["domain"]
    history, feedback, evaluation = [], "", {}

    while len(history) < MAX_STEPS:
        context = COMPLETED_MARKER + json.dumps(history) if history else ""
        plan = _post(gateway, "planner", {"goal": goal, "context": context, "feedback": feedback})["plan"]
        step = _post(gateway, "actor", {
            "goal": goal, "plan": plan, "step_id": plan[0]["id"],
            "history": history, "domain": domain,
        })
        history.append(step)
        evaluation = _post(gateway, "evaluator", {"goal": goal, "plan": plan, "history": history})
        feedback = evaluation.get("feedback", "")
        print(f"step {step.get('step_id')} [{step.get('status')}] {step.get('description')}")
        print(f"  -> {evaluation.get('verdict')}: {feedback[:200]}")
        if evaluation.get("verdict") == "done":
            break

    return evaluation, history


def main():
    p = argparse.ArgumentParser()
    p.add_argument("workload")
    p.add_argument("--gateway", help="real faasd gateway URL; omit to run everything in-process")
    args = p.parse_args()

    with open(args.workload) as f:
        workload = json.load(f)
    gateway = (args.gateway or _start_local_gateway()).rstrip("/")

    _call(gateway, workload["tool_function"], {"action": "reset"})
    evaluation, history = run(workload, gateway)

    answer = evaluation.get("feedback", "") if evaluation.get("verdict") == "done" else ""
    print("\nfinal answer:", answer or "(loop ended without 'done')")
    result = score(workload, gateway, answer)
    print(json.dumps(result, indent=2))
    return 0 if result["reward"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
