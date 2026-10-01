"""
Figures and a metrics table for the real-LLM (Groq) runs of Experiment 1,
one result folder per configuration:

  python experiments/plots_groq.py \\
      --config "Conductor + faasd|gpt-oss-120b|experiments/results/exp1-conductor-20260930-173536" \\
      --config "Argo + Knative, 2 s requeue|gpt-oss-120b|experiments/results/exp1-argo-20260930-180520" \\
      --config "Argo + Knative, 10 s requeue|gpt-oss-20b|experiments/results/exp1-argo-20261001-054740" \\
      --out experiments/plots/groq

Each --config is "label|model|folder[,folder...]"; the first config gets
categorical slot 1 (blue), the second slot 3 (green) and the third slot 2
(orange), matching plots.py (Conductor blue, tuned Argo green, default Argo
orange). Only runs with reward 1 are used.

With a hosted API, T_LLM includes rate-limit waits (Groq 429 + Retry-After),
so the figures split them out and use Rfriction_net. Torch, Troute and Twarm
don't depend on the model; T_LLM and Rfriction do, so configurations run
with different models are only compared on the former.

Needs: pip install matplotlib
"""
import argparse
import csv
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plots import (INK, PART_COLORS, STACK_COLORS, SURFACE, Stack, _f, _jitter,  # noqa: E402
                   _log_axis, _read, _save, plt)

SLOTS = [STACK_COLORS[0], STACK_COLORS[2], STACK_COLORS[1]]
WAIT_COLOR = "#c9c7bf"  # neutral: the wait is the API's, not either stack's


class Config(Stack):
    def __init__(self, spec, color):
        label, model, folders = spec.split("|")
        self.folders = folders.split(",")
        super().__init__(label, self.folders, color)
        self.model = model
        self.name = f"{label}\n({model})"

    def total_runs_of(self, workload=None):
        """All runs of a workload, successful or not (for "runs used")."""
        return [r for r in _read(self.folders, "dataplane_runs.csv") if workload in (None, r["workload"])]

    def runs_of(self, task):
        return [r for r in self.runs if task in (None, r["workload"])]

    def per_turn(self, key, task=None):
        return [_f(r[key]) / int(r["n_turns"]) for r in self.runs_of(task) if int(r["n_turns"] or 0) > 0]

    def tool_calls(self, key, task=None):
        return [_f(c[key]) for c in self.calls if c["kind"] == "tool" and task in (None, c["workload"])
                and _f(c[key]) is not None]

    def task_vals(self, key, task=None):
        return [_f(r[key]) for r in self.runs_of(task) if _f(r[key]) is not None]


def _tasks(configs):
    return sorted({r["workload"] for c in configs for r in c.runs})


def _sec(ms):
    return ms / 1000.0


def _mean(xs):
    return statistics.fmean(xs) if xs else 0.0


def _strip(configs, values_of, xlabel, title, fmt, log=False):
    """One panel per task, configurations as rows; values_of(config, task) -> list."""
    tasks = _tasks(configs)
    fig, axes = plt.subplots(1, len(tasks), figsize=(5.2 * len(tasks) + 1.5, 3.0), sharey=True, squeeze=False)
    for ax, task in zip(axes[0], tasks):
        for ci, c in enumerate(configs):
            vals = values_of(c, task)
            if not vals:
                ax.text(0.5, ci, "no successful runs", transform=ax.get_yaxis_transform(), ha="center",
                        va="center", fontsize=8, color="#52514e")
                continue
            ax.scatter(vals, [ci + _jitter(i % 7, 7, 0.3) for i in range(len(vals))], s=26, color=c.color,
                       alpha=0.8, edgecolors=SURFACE, linewidths=0.8, zorder=3)
            med = statistics.median(vals)
            ax.plot([med, med], [ci - 0.28, ci + 0.28], color=INK, lw=2, zorder=4)
            ax.text(med, ci - 0.34, f"median {fmt(med)}", ha="center", va="bottom", fontsize=9,
                    bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.9))
        if log:
            ax.set_xscale("log")
            _log_axis(ax.xaxis)
        ax.set_yticks(range(len(configs)), [c.name for c in configs])
        ax.set_ylim(len(configs) - 0.5, -0.75)
        ax.grid(axis="y", visible=False)
        ax.set_title(task, fontsize=10)
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=12)
    fig.supxlabel(xlabel, fontsize=10, color="#52514e")
    fig.tight_layout()
    return fig, axes[0]


def _model_note(fig, configs, y=-0.04):
    """Footnote when configurations used different models: LLM time differs by model."""
    if len({c.model for c in configs}) > 1:
        fig.text(0.01, y, "Configurations use different models (in brackets): LLM time and Rfriction compare "
                 "only within one model; Torch and Troute don't depend on the model.", fontsize=8, color="#52514e")


def fig_breakdown(configs, out):
    """Mean Te2e per run, split into what each part of the system spent."""
    parts = [
        ("LLM (model time)", PART_COLORS[0], lambda r: _f(r["t_llm_ms"]) - _f(r["t_llm_wait_ms"] or 0)),
        ("LLM rate-limit wait (Groq 429)", WAIT_COLOR, lambda r: _f(r["t_llm_wait_ms"] or 0)),
        ("Torch (orchestrator)", PART_COLORS[1], lambda r: _f(r["torch_run_ms"])),
        ("Troute (routing)", PART_COLORS[2], lambda r: _f(r["troute_orch_ms"]) + _f(r["troute_tool_ms"])),
    ]

    measured = list(parts)

    def other(r):  # function work outside the model: tool calls, parsing, ...
        return _f(r["te2e_ms"]) - sum(fn(r) for _, _, fn in measured)

    parts.append(("functions' own work (tools, parsing)", PART_COLORS[3], other))
    rows = [(c, t) for c in configs for t in _tasks(configs)]
    fig, ax = plt.subplots(figsize=(9, 0.55 * len(rows) + 1.6))
    for ri, (c, task) in enumerate(rows):
        runs = c.runs_of(task)
        if not runs:
            ax.text(0, ri, "  no successful runs", va="center", fontsize=9, color="#52514e")
            continue
        left = 0.0
        for name, color, fn in parts:
            v = _sec(_mean([fn(r) for r in runs]))
            ax.barh(ri, v, left=left, color=color, height=0.6, edgecolor=SURFACE, linewidth=2,
                    hatch="//" if color == WAIT_COLOR else None, label=name if ri == 0 else None)
            left += v
        ax.text(left, ri, f"  {left:.0f} s", va="center", fontsize=9, color=INK)
    ax.set_yticks(range(len(rows)), [f"{c.label} ({c.model})  ·  {t}" for c, t in rows], fontsize=8.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, ax.get_xlim()[1] * 1.08)
    ax.set_xlabel("mean end-to-end time per run, Te2e (s)")
    ax.set_title("Where a run's time goes, with a real LLM")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3)
    _model_note(fig, configs, y=-0.1)
    _save(fig, out, "figG1_breakdown")


def fig_torch(configs, out):
    fig, _ = _strip(configs, lambda c, t: [_sec(v) for v in c.per_turn("torch_run_ms", t)],
           "orchestration overhead per turn, Torch_run / turns (s, log scale)",
           "Orchestrator overhead per agent turn", lambda v: f"{v:.1f} s", log=True)
    _save(fig, out, "figG2_torch_per_turn")


def fig_troute(configs, out):
    fig, axes = _strip(configs, lambda c, t: c.tool_calls("troute_ms", t),
           "routing overhead per actor → tool call, Troute (ms)",
           "FaaS routing overhead per tool call", lambda v: f"{v:.1f} ms")
    for ax in axes:
        ax.set_xlim(left=0)
    _save(fig, out, "figG3_troute_tool")


def fig_rfriction(configs, out):
    fig, _ = _strip(configs, lambda c, t: c.task_vals("rfriction_net", t),
           "Rfriction_net = (Torch_run + ΣTroute) / (T_LLM − rate-limit wait)  (log scale)",
           "System friction relative to the model's own time", lambda v: f"{v:.2f}", log=True)
    _model_note(fig, configs, y=-0.06)
    _save(fig, out, "figG4_rfriction_net")


def metrics_table(configs, out):
    def med(xs):
        return statistics.median(xs) if xs else None

    rows = []
    for c in configs:
        for wl in [None] + sorted({r["workload"] for r in c.runs}):
            runs = [r for r in c.runs if wl in (None, r["workload"])]
            keys = {(r["_folder"], r["workload"], r["run"]) for r in runs}
            tools = [x for x in c.calls if x["kind"] == "tool" and (x["_folder"], x["workload"], x["run"]) in keys]
            total = len(c.total_runs_of(wl))
            rows.append({
                "configuration": c.label, "model": c.model, "workload": wl or "all",
                "runs_used": f"{len(runs)}/{total}",
                "turns_mean": _mean([int(r["n_turns"]) for r in runs]),
                "Te2e_s": med([_sec(_f(r["te2e_ms"])) for r in runs]),
                "T_LLM_s": med([_sec(_f(r["t_llm_ms"])) for r in runs]),
                "T_LLM_wait_s": med([_sec(_f(r["t_llm_wait_ms"] or 0)) for r in runs]),
                "Torch_per_turn_s": med([_sec(_f(r["torch_run_ms"]) / int(r["n_turns"])) for r in runs]),
                "Troute_tool_ms": med([_f(x["troute_ms"]) for x in tools]),
                "Twarm_tool_ms": med([_f(x["twarm_ms"]) for x in tools]),
                "Rfriction": med([_f(r["rfriction"]) for r in runs]),
                "Rfriction_net": med([_f(r["rfriction_net"]) for r in runs]),
            })
    with open(os.path.join(out, "metrics.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()} for r in rows)

    def cell(v, nd):
        return "–" if v is None else f"{v:.{nd}f}"

    lines = ["| Configuration | Model | Workload | Runs used | Turns | Te2e (s) | T_LLM (s) | of which rate-limit wait (s) "
             "| Torch / turn (s) | Troute / tool call (ms) | Twarm / tool call (ms) | Rfriction | Rfriction_net |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['configuration']} | {r['model']} | {r['workload']} | {r['runs_used']} | "
                     f"{cell(r['turns_mean'], 1)} | {cell(r['Te2e_s'], 1)} | {cell(r['T_LLM_s'], 1)} | "
                     f"{cell(r['T_LLM_wait_s'], 1)} | {cell(r['Torch_per_turn_s'], 2)} | "
                     f"{cell(r['Troute_tool_ms'], 1)} | {cell(r['Twarm_tool_ms'], 2)} | "
                     f"{cell(r['Rfriction'], 2)} | {cell(r['Rfriction_net'], 2)} |")
    with open(os.path.join(out, "metrics.md"), "w") as f:
        f.write("Medians over runs with reward 1 (Troute/Twarm: over tool calls).\n\n" + "\n".join(lines) + "\n")
    print("wrote", os.path.join(out, "metrics.md"))
    print("\n".join(lines))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", action="append", required=True, help='"label|model|folder[,folder...]"')
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "plots", "groq"))
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)
    configs = [Config(spec, SLOTS[i]) for i, spec in enumerate(args.config)]
    for c in configs:
        print(f"{c.label} ({c.model}): {len(c.runs)}/{c.total_runs} runs with reward 1")
    fig_breakdown(configs, args.out)
    fig_torch(configs, args.out)
    fig_troute(configs, args.out)
    fig_rfriction(configs, args.out)
    metrics_table(configs, args.out)


if __name__ == "__main__":
    main()
