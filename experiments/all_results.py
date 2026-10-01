"""
Every number collected so far, per configuration and per task, in one place:
writes experiments/plots/ALL_RESULTS.md and all_results.csv (medians), plus the
unaggregated data: all_runs.csv (one row per run), all_tool_calls.csv (one row
per actor -> tool call) and all_cold_trials.csv (one row per Exp 2b trial).

  python experiments/all_results.py

The result folders of each dataset are listed in DATASETS / COLD below; add a
folder there when a new run is pushed. Exp 1 numbers are medians over the runs
that scored reward 1 (tool-call metrics: over the tool calls of those runs).
"""
import csv
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
OUT = os.path.join(HERE, "plots")

C, A2, A10 = "Conductor + faasd", "Argo + Knative, 2 s requeue", "Argo + Knative, 10 s requeue"

# (dataset title, note, [(configuration, folder), ...])
DATASETS = [
    ("Mock LLM, laptop (WSL2)",
     "Mock LLM sleeps 2 s / 2 s / 1 s per planner / actor / evaluator call. "
     "Laptop k3s ran with a DNS workaround that inflated Knative routing; prefer the cluster numbers.",
     [(C, "exp1-conductor-20260927-110845"), (A10, "exp1-argo-20260927-181019")]),
    ("Mock LLM, cluster (node13 VM)",
     "Same mock LLM. 10 runs per task after 1 warm-up.",
     [(C, "exp1-conductor-20260929-135611"), (A10, "exp1-argo-20260929-213732"),
      (A2, "exp1-argo-20260929-234613")]),
    ("Real LLM (Groq gpt-oss-120b), cluster VM",
     "5 runs per task, no warm-up, 60 s pause. Argo 10 s: airline-status runs all hit Groq's "
     "daily token limit, so only retail-status has data.",
     [(C, "exp1-conductor-20260930-173536"), (A2, "exp1-argo-20260930-180520"),
      (A10, "exp1-argo-20260930-183000")]),
    ("Real LLM (Groq gpt-oss-20b), cluster VM",
     "5 runs per task, no warm-up, 60 s pause. Argo 2 s: retail-status run 5 hit Groq's daily token limit.",
     [(C, "exp1-conductor-20261001-072147"), (A2, "exp1-argo-20261001-064730"),
      (A10, "exp1-argo-20261001-054740")]),
]

COLD = [
    ("Laptop (WSL2)", [(C, "exp2b-conductor-20260928-104555"), (A2, "exp2b-argo-20260928-115146")]),
    ("Cluster (node13 VM)", [(C, "exp2b-conductor-20260929-144643"), (A2, "exp2b-argo-20260930-035225")]),
]


def _rows(folder, name):
    path = os.path.join(RES, folder, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def exp1_rows():
    out = []
    for title, _, configs in DATASETS:
        for config, folder in configs:
            runs = _rows(folder, "dataplane_runs.csv")
            turns = _rows(folder, "dataplane_turns.csv")
            calls = _rows(folder, "dataplane_calls.csv")
            sw = {(r["workload"], r["run"]): r for r in _rows(folder, "sworkflow_runs.csv")}
            for wl in sorted({r["workload"] for r in runs}):
                every = [r for r in runs if r["workload"] == wl]
                ok = [r for r in every if (r.get("status") or r.get("phase")) in ("COMPLETED", "Succeeded")
                      and _f(r.get("reward")) == 1.0 and r.get("missing_timing") in ("False", "", None)]
                keys = {r["run"] for r in ok}
                t = [x for x in turns if x["workload"] == wl and x["run"] in keys]
                tools = [x for x in calls if x["workload"] == wl and x["run"] in keys and x["kind"] == "tool"]
                kb = [_f(sw[(wl, k)]["total_bytes"]) / 1024 for k in keys if (wl, k) in sw]
                out.append({
                    "dataset": title, "configuration": config, "task": wl, "folder": folder,
                    "runs_used": len(ok), "runs_total": len(every),
                    "turns": _med([_f(r["n_turns"]) for r in ok]),
                    "Te2e_s": _med([_f(r["te2e_ms"]) / 1000 for r in ok]),
                    "T_LLM_s": _med([_f(r["t_llm_ms"]) / 1000 for r in ok]),
                    "T_LLM_wait_s": _med([(_f(r.get("t_llm_wait_ms")) or 0) / 1000 for r in ok]),
                    "Torch_turn_s": _med([_f(x["torch_ms"]) / 1000 if _f(x["torch_ms"]) is not None else None
                                          for x in t]),
                    "Torch_run_s": _med([_f(r["torch_run_ms"]) / 1000 for r in ok]),
                    "Troute_orch_ms": _med([_f(r["troute_orch_ms"]) for r in ok]),
                    "Troute_tool_ms": _med([_f(x["troute_ms"]) for x in tools]),
                    "Twarm_tool_ms": _med([_f(x["twarm_ms"]) for x in tools]),
                    "Rfriction": _med([_f(r["rfriction"]) for r in ok]),
                    # older (mock) folders predate rfriction_net; without waits it equals Rfriction
                    "Rfriction_net": _med([_f(r.get("rfriction_net") or r["rfriction"]) for r in ok]),
                    "Sworkflow_KB": _med(kb),
                })
    return out


def cold_rows():
    out = []
    for title, configs in COLD:
        for config, folder in configs:
            trials = _rows(folder, "trials.csv")
            for wl in sorted({r["workload"] for r in trials}):
                tr = [r for r in trials if r["workload"] == wl]
                out.append({
                    "dataset": title, "configuration": config, "task": wl, "folder": folder,
                    "trials": len(tr),
                    "Tcold_ms": _med([_f(r["tcold_tool_ms"]) for r in tr]),
                    "first_call_ms": _med([_f(r["cold_first_call_http_ms"]) for r in tr]),
                    "warm_call_ms": _med([_f(r["warm_tool_http_median_ms"]) for r in tr]),
                    "app_init_ms": _med([_f(r["cold_handler_ms"]) for r in tr]),
                    "attempts": _med([_f(r["first_call_attempts"]) for r in tr]),
                    "dTe2e_s": _med([_f(r["delta_te2e_ms"]) / 1000 if _f(r["delta_te2e_ms"]) is not None else None
                                     for r in tr]),
                })
    return out


def raw_exports():
    """Every run (all_runs.csv) and every tool call (all_tool_calls.csv) of the
    folders above, unaggregated, labelled with dataset / configuration / task.
    used = the run scored reward 1 and is in the medians."""
    runs_out, calls_out, cold_out = [], [], []
    for title, _, configs in DATASETS:
        for config, folder in configs:
            sw = {(r["workload"], r["run"]): r for r in _rows(folder, "sworkflow_runs.csv")}
            used = set()
            for r in _rows(folder, "dataplane_runs.csv"):
                ok = ((r.get("status") or r.get("phase")) in ("COMPLETED", "Succeeded")
                      and _f(r.get("reward")) == 1.0 and r.get("missing_timing") in ("False", "", None))
                if ok:
                    used.add((r["workload"], r["run"]))
                runs_out.append({
                    "dataset": title, "configuration": config, "folder": folder, "task": r["workload"],
                    "run": r["run"], "used": ok, "outcome": r.get("status") or r.get("phase"),
                    "reward": r.get("reward"), "turns": r["n_turns"], "te2e_ms": r["te2e_ms"],
                    "t_llm_ms": r["t_llm_ms"], "t_llm_wait_ms": r.get("t_llm_wait_ms") or 0,
                    "torch_run_ms": r["torch_run_ms"], "troute_orch_ms": r["troute_orch_ms"],
                    "troute_tool_ms": r["troute_tool_ms"], "twarm_tool_mean_ms": r.get("twarm_tool_mean_ms"),
                    "troute_tool_mean_ms": r.get("troute_tool_mean_ms"), "rfriction": r["rfriction"],
                    "rfriction_net": r.get("rfriction_net") or r["rfriction"],
                    "sworkflow_bytes": (sw.get((r["workload"], r["run"])) or {}).get("total_bytes"),
                })
            for c in _rows(folder, "dataplane_calls.csv"):
                if c["kind"] == "tool":
                    calls_out.append({
                        "dataset": title, "configuration": config, "folder": folder, "task": c["workload"],
                        "run": c["run"], "used": (c["workload"], c["run"]) in used, "turn": c["turn"],
                        "tool": c["name"], "twarm_ms": c["twarm_ms"], "troute_ms": c["troute_ms"],
                    })
    for title, configs in COLD:
        for config, folder in configs:
            for r in _rows(folder, "trials.csv"):
                cold_out.append({"dataset": title, "configuration": config, "folder": folder, **r})
    for name, rows in (("all_runs.csv", runs_out), ("all_tool_calls.csv", calls_out),
                       ("all_cold_trials.csv", cold_out)):
        with open(os.path.join(OUT, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {name}: {len(rows)} rows")


def _cell(v, nd):
    return "–" if v is None else f"{v:,.{nd}f}"


def main():
    e1, e2 = exp1_rows(), cold_rows()
    with open(os.path.join(OUT, "all_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(e1[0]))
        w.writeheader()
        w.writerows(e1)
    with open(os.path.join(OUT, "all_results_cold.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(e2[0]))
        w.writeheader()
        w.writerows(e2)

    md = ["# All results so far",
          "",
          "Generated by `experiments/all_results.py` from `experiments/results/`. Medians over the runs "
          "that scored reward 1; tool-call metrics (Troute, Twarm) over those runs' tool calls.",
          "",
          "- **Torch / turn**: orchestration between two planner calls (needs ≥ 2 turns; – for 1-turn runs). "
          "**Torch_run**: all orchestration in the run, including workflow start and finish.",
          "- **Troute orch**: routing on orchestrator → function hops per run (Argo: estimated from the "
          "tool-call median, see `dataplane.py`). **Troute tool**: per actor → tool call.",
          "- **Rfriction** = (Torch_run + ΣTroute) / T_LLM. **Rfriction_net** leaves Groq's rate-limit "
          "waits out of T_LLM (same as Rfriction with the mock).",
          "- Real-LLM runs: T_LLM and Rfriction depend on the model, so compare them within one model. "
          "Torch, Troute and Twarm don't.",
          ""]
    hdr = ("| Configuration | Task | Runs | Turns | Te2e (s) | T_LLM (s) | rate-limit wait (s) | Torch / turn (s) "
           "| Torch_run (s) | Troute orch (ms/run) | Troute tool (ms) | Twarm tool (ms) | Rfriction "
           "| Rfriction_net | Sworkflow (KB) |")
    sep = "|" + "---|" * 15
    for title, note, _ in DATASETS:
        md += [f"## Experiment 1: {title}", "", note, "", hdr, sep]
        for r in [r for r in e1 if r["dataset"] == title]:
            md.append(f"| {r['configuration']} | {r['task']} | {r['runs_used']}/{r['runs_total']} | "
                      f"{_cell(r['turns'], 0)} | {_cell(r['Te2e_s'], 1)} | {_cell(r['T_LLM_s'], 1)} | "
                      f"{_cell(r['T_LLM_wait_s'], 1)} | {_cell(r['Torch_turn_s'], 2)} | {_cell(r['Torch_run_s'], 1)} | "
                      f"{_cell(r['Troute_orch_ms'], 0)} | {_cell(r['Troute_tool_ms'], 1)} | "
                      f"{_cell(r['Twarm_tool_ms'], 2)} | {_cell(r['Rfriction'], 2)} | "
                      f"{_cell(r['Rfriction_net'], 2)} | {_cell(r['Sworkflow_KB'], 0)} |")
        folders = sorted({(c, f) for c, f in next(d for d in DATASETS if d[0] == title)[2]})
        md += ["", "Folders: " + "; ".join(f"{c}: `{f}`" for c, f in folders), ""]

    md += ["## Experiment 2b: cold start of a tool function (mock LLM)", "",
           "Tcold = first call to a cold tool − its warm round trip, measured by the actor. "
           "Argo ran with a 2 s requeue. First call: round trip of the first (cold) call; "
           "app init: the tool's handler time on that call; attempts: requests the actor sent "
           "before the cold tool answered; ΔTe2e: cold run − warm run.", "",
           "| Machine | Configuration | Task | Trials | Tcold (ms) | First call (ms) | Warm call (ms) "
           "| App init (ms) | Attempts | ΔTe2e (s) |",
           "|" + "---|" * 10]
    for r in e2:
        md.append(f"| {r['dataset']} | {r['configuration']} | {r['task']} | {r['trials']} | "
                  f"{_cell(r['Tcold_ms'], 0)} | {_cell(r['first_call_ms'], 0)} | {_cell(r['warm_call_ms'], 1)} | "
                  f"{_cell(r['app_init_ms'], 0)} | {_cell(r['attempts'], 0)} | {_cell(r['dTe2e_s'], 2)} |")
    md += ["", "Folders: " + "; ".join(f"{t} {c}: `{f}`" for t, cs in COLD for c, f in cs), ""]
    with open(os.path.join(OUT, "ALL_RESULTS.md"), "w") as f:
        f.write("\n".join(md))
    print("\n".join(md))
    raw_exports()


if __name__ == "__main__":
    main()
