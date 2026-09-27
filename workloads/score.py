"""
Score a workload run the way tau-bench does, against a deployed tool server.

tau-bench gives reward 1 only if BOTH hold:
  1. the tool server's DB ends in exactly the expected state (hash match) -
     i.e. the right writes happened, and no extra/wrong ones;
  2. every string in `expected_outputs` appears in the agent's final answer
     (case-insensitive, commas stripped).

Usage:
  # before a run: put the DB back in its original state
  python workloads/score.py workloads/retail-44.json --gateway http://<faasd-host>:8080 --reset

  # after a run: score it (final answer as text, or a file containing it)
  python workloads/score.py workloads/retail-44.json --gateway http://<faasd-host>:8080 \\
      --answer "Done. You get back 17.99 to your gift card."

--gateway is either
  http://<faasd-host>:8080                      OpenFaaS: POST <gateway>/function/<fn>
  knative://<ingress-host:port>/<domain-suffix>  Knative: POST http://<ingress>/ with
                                                 Host: <fn>.<domain-suffix>, e.g.
                                                 knative://10.0.0.5:80/default.example.com

Stdlib only, so it runs anywhere.
"""
import argparse
import json
import os
import sys
import urllib.request


def _target(gateway, function):
    """(url, extra headers) for calling `function` through `gateway`."""
    if gateway.startswith("knative://"):
        ingress, _, domain = gateway[len("knative://"):].partition("/")
        return f"http://{ingress}/", {"Host": f"{function}.{domain.strip('/')}"}
    return f"{gateway.rstrip('/')}/function/{function}", {}


def _call(gateway, function, body):
    url, headers = _target(gateway, function)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def score(workload, gateway, answer):
    normalized = answer.lower().replace(",", "")
    db_hash = _call(gateway, workload["tool_function"], {"action": "hash"})["hash"]
    db_ok = db_hash == workload["expected_db_hash"]
    outputs = {o: o.lower() in normalized for o in workload["expected_outputs"]}
    return {
        "workload": workload["id"],
        "reward": 1.0 if db_ok and all(outputs.values()) else 0.0,
        "db_state_correct": db_ok,
        "db_hash": db_hash,
        "outputs_found": outputs,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("workload", help="path to a workloads/*.json file")
    p.add_argument("--gateway", default=os.environ.get("OPENFAAS_URL", "http://127.0.0.1:8080"))
    p.add_argument("--reset", action="store_true", help="reset the DB and exit")
    p.add_argument("--answer", default="", help="final answer text, or a path to a file with it")
    args = p.parse_args()

    with open(args.workload) as f:
        w = json.load(f)
    fn = w["tool_function"]

    if args.reset:
        print(_call(args.gateway, fn, {"action": "reset"}))
        return 0

    answer = args.answer
    if answer and os.path.isfile(answer):
        with open(answer) as f:
            answer = f.read()
    result = score(w, args.gateway, answer)
    print(json.dumps(result, indent=2))
    return 0 if result["reward"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
