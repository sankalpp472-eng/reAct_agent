"""
Figures comparing the two stacks, from Experiment 1 result folders.

  python experiments/plots.py \\
      --conductor experiments/results/exp1-conductor-20260927-110845 \\
      --argo      experiments/results/exp1-argo-20260927-181019 \\
      [--argo-tuned experiments/results/exp1-argo-<ts-with-2s-requeue>] \\
      --out experiments/plots

Each folder needs dataplane_runs.csv / dataplane_turns.csv / dataplane_calls.csv
(written by the drivers, or by recompute_dataplane.py) and, for figure 6,
sworkflow_runs.csv (sworkflow.py). Several folders per stack are merged.
Writes PNG (for slides) and PDF (for the paper) per figure.

Needs: pip install matplotlib
"""
import argparse
import csv
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# ---- palette (dataviz reference palette, light mode; validated) --------------
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de"
STACK_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]          # categorical slots 1-3
PART_COLORS = ["#1baf7a", "#4a3aa7", "#e87ba4", "#eda100"]  # validated set for components
FN_COLORS = {"planner": "#1baf7a", "actor": "#4a3aa7", "evaluator": "#e87ba4"}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "text.color": INK,
    "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.axisbelow": True, "axes.spines.top": False,
    "axes.spines.right": False, "font.size": 10, "axes.titlesize": 12,
    "axes.titleweight": "bold", "axes.titlelocation": "left", "legend.frameon": False,
    "legend.fontsize": 9,
})
WORKLOADS = ["retail-44", "airline-26"]


# ---- data ---------------------------------------------------------------------

def _read(folders, name):
    rows = []
    for folder in folders:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path) as f:
                for r in csv.DictReader(f):
                    r["_folder"] = folder
                    rows.append(r)
    return rows


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Stack:
    def __init__(self, label, folders, color):
        self.label, self.color = label, color
        runs = _read(folders, "dataplane_runs.csv")
        ok_status = lambda r: (r.get("status") or r.get("phase")) in ("COMPLETED", "Succeeded")  # noqa: E731
        self.runs = [r for r in runs if ok_status(r) and _f(r.get("reward")) == 1.0
                     and r.get("missing_timing") in ("False", None, "")]
        self.total_runs = len(runs)
        keys = {(r["_folder"], r["workload"], r["run"]) for r in self.runs}
        keep = lambda r: (r["_folder"], r["workload"], r["run"]) in keys  # noqa: E731
        self.turns = [t for t in _read(folders, "dataplane_turns.csv") if keep(t)]
        self.calls = [c for c in _read(folders, "dataplane_calls.csv") if keep(c)]
        self.sw = [s for s in _read(folders, "sworkflow_runs.csv") if s.get("used") == "True"]

    def run_vals(self, key, workload=None):
        return [_f(r[key]) for r in self.runs if workload in (None, r["workload"]) and _f(r[key]) is not None]


def _save(fig, out, name):
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("wrote", os.path.join(out, name + ".png"))


def _sec(ms):
    return ms / 1000.0


def _jitter(i, n, width=0.18):
    return (i - (n - 1) / 2) / max(n - 1, 1) * width if n > 1 else 0.0


# ---- figures ------------------------------------------------------------------

def fig_te2e(stacks, out):
    fig, ax = plt.subplots(figsize=(7, 3.6))
    w = 0.8 / len(stacks)
    for si, s in enumerate(stacks):
        for wi, wl in enumerate(WORKLOADS):
            vals = [_sec(v) for v in s.run_vals("te2e_ms", wl)]
            if not vals:
                continue
            x = wi + (si - (len(stacks) - 1) / 2) * w
            ax.scatter([x + _jitter(i, len(vals), w * 0.6) for i in range(len(vals))], vals,
                       s=22, color=s.color, alpha=0.75, edgecolors=SURFACE, linewidths=0.8,
                       label=s.label if wi == 0 else None, zorder=3)
            med = statistics.median(vals)
            ax.plot([x - w * 0.38, x + w * 0.38], [med, med], color=INK, lw=2, zorder=4)
            ax.annotate(f"{med:.0f} s", (x + w * 0.4, med), xytext=(3, 0), textcoords="offset points",
                        va="center", fontsize=9, color=INK)
    ax.set_xticks(range(len(WORKLOADS)), WORKLOADS)
    ax.set_ylabel("end-to-end latency, Te2e (s)")
    ax.set_ylim(bottom=0)
    ax.set_title("End-to-end task latency per run")
    ax.legend(loc="upper left")
    ax.text(0, -0.2, "Dots: individual runs. Bars: median. Same functions, images and mock LLM on both stacks.",
            transform=ax.transAxes, fontsize=8, color=INK2)
    _save(fig, out, "fig1_te2e")


def fig_breakdown(stacks, out):
    parts = [("LLM time", "t_llm"), ("function work (non-LLM)", "fn"),
             ("routing (orchestrator→function)", "troute_orch_ms"), ("orchestration (Torch_run)", "torch_run_ms")]
    rows = []
    for wl in WORKLOADS:
        for s in stacks:
            rs = [r for r in s.runs if r["workload"] == wl]
            if not rs:
                continue
            m = lambda k: statistics.fmean(_f(r[k]) for r in rs)  # noqa: E731
            te2e, llm, orch, route = m("te2e_ms"), m("t_llm_ms"), m("torch_run_ms"), m("troute_orch_ms")
            rows.append((f"{s.label}\n{wl}", [llm, te2e - llm - orch - route, route, orch], te2e))
    xmax = max(_sec(r[2]) for r in rows)
    fig, ax = plt.subplots(figsize=(8, 0.75 * len(rows) + 1.4))
    for yi, (label, vals, te2e) in enumerate(rows):
        left = 0.0
        for pi, v in enumerate(vals):
            ax.barh(yi, _sec(v), left=_sec(left), color=PART_COLORS[pi], edgecolor=SURFACE, linewidth=2,
                    height=0.6, label=parts[pi][0] if yi == 0 else None)
            share = v / te2e
            if _sec(v) >= 0.08 * xmax:  # label only segments wide enough to hold it
                ax.text(_sec(left + v / 2), yi, f"{share:.0%}", ha="center", va="center", fontsize=8,
                        color=INK)
            left += v
        ax.annotate(f"{_sec(te2e):.0f} s", (_sec(te2e), yi), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel("mean time per run (s)")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, xmax * 1.1)
    ax.set_title("Where the time goes", pad=46)
    ax.legend(ncol=2, loc="lower left", bbox_to_anchor=(0, 1.0))
    _save(fig, out, "fig2_breakdown")


def fig_torch_turn(stacks, out):
    fig, ax = plt.subplots(figsize=(7, 1.0 + 0.9 * len(stacks)))
    for si, s in enumerate(stacks):
        vals = [_sec(_f(t["torch_ms"])) for t in s.turns if _f(t.get("torch_ms")) is not None]
        if not vals:
            continue
        ax.scatter(vals, [si + _jitter(i % 9, 9, 0.35) for i in range(len(vals))], s=14, color=s.color,
                   alpha=0.6, edgecolors="none", zorder=3)
        med = statistics.median(vals)
        ax.plot([med, med], [si - 0.3, si + 0.3], color=INK, lw=2, zorder=4)
        ax.text(med, si - 0.36, f"median {med:.2f} s" if med < 10 else f"median {med:.0f} s",
                ha="center", va="bottom", fontsize=9, bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.9))
    for k in range(1, 9):  # Argo's default requeue period is 10 s
        ax.axvline(10 * k, color=INK2, lw=0.8, ls=(0, (2, 3)), zorder=1)
    ax.text(10, len(stacks) - 0.45, "dotted: multiples of 10 s (Argo's default requeue period)",
            fontsize=8, color=INK2, va="bottom")
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g} s"))
    ax.set_yticks(range(len(stacks)), [s.label for s in stacks])
    ax.set_ylim(len(stacks) - 0.4, -0.65)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("orchestration time per loop turn, Torch (log scale)")
    ax.set_title("Orchestration overhead per agent turn")
    _save(fig, out, "fig3_torch_per_turn")


def fig_timeline(stacks, out, workload="retail-44"):
    picks = []
    for s in stacks:
        rs = sorted((r for r in s.runs if r["workload"] == workload), key=lambda r: _f(r["te2e_ms"]))
        if rs:
            median_run = rs[len(rs) // 2]
            calls = sorted((c for c in s.calls if c["kind"] == "function" and c["workload"] == workload
                            and c["run"] == median_run["run"] and c["_folder"] == median_run["_folder"]),
                           key=lambda c: _f(c["t3"]))
            picks.append((s, calls, _f(median_run["te2e_ms"])))
    if not picks:
        return
    xmax = max(_sec(max(_f(c["t3"]) + _f(c["twarm_ms"]) for c in calls) - _f(calls[0]["t3"]))
               for _, calls, _ in picks) * 1.04
    fig, axes = plt.subplots(len(picks), 1, figsize=(9, 0.9 * len(picks) + 1.2), sharex=True, squeeze=False)
    for ax, (s, calls, te2e) in zip(axes[:, 0], picks):
        t_origin = _f(calls[0]["t3"])
        for c in calls:
            ax.barh(0, _sec(_f(c["twarm_ms"])), left=_sec(_f(c["t3"]) - t_origin), height=0.55,
                    color=FN_COLORS[c["name"]], edgecolor=SURFACE, linewidth=1)
        ax.set_yticks([0], [s.label])
        ax.set_xlim(0, xmax)
        ax.grid(axis="y", visible=False)
        end = _sec(_f(calls[-1]["t3"]) + _f(calls[-1]["twarm_ms"]) - t_origin)
        ax.annotate(f"done at {end:.0f} s", (end, 0), xytext=(6, 0), textcoords="offset points",
                    fontsize=9, color=INK, va="center")
    handles = [plt.Rectangle((0, 0), 1, 1, color=col) for col in FN_COLORS.values()]
    axes[0, 0].legend(handles, list(FN_COLORS), ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0))
    axes[-1, 0].set_xlabel("seconds since the first planner call (median run)")
    fig.suptitle(f"One {workload} run: function calls (bars) and the gaps between them", x=0.01, ha="left",
                 fontweight="bold", fontsize=12, y=1.02 + 0.03 * (3 - len(picks)))
    fig.text(0.01, -0.1, "Gaps between bars are time outside the functions: orchestrator scheduling, "
             "state updates and request routing.", fontsize=8, color=INK2)
    _save(fig, out, "fig4_timeline")


def fig_route_ecdf(stacks, out):
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for s in stacks:
        vals = sorted(_f(c["troute_ms"]) for c in s.calls
                      if c["kind"] == "tool" and _f(c.get("troute_ms")) is not None)
        if not vals:
            continue
        ys = [(i + 1) / len(vals) for i in range(len(vals))]
        ax.step(vals, ys, where="post", color=s.color, lw=2, label=f"{s.label} (n={len(vals)})")
        med = statistics.median(vals)
        ax.plot([med], [0.5], "o", ms=8, color=s.color, markeredgecolor=SURFACE, markeredgewidth=2, zorder=4)
        ax.annotate(f"median {med:.0f} ms", (med, 0.5), xytext=(6, -12), textcoords="offset points",
                    fontsize=9)
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g} ms"))
    ax.set_xlabel("routing time per tool call, Troute = (t5 − t2) − Twarm (ms, log scale)")
    ax.set_ylabel("fraction of tool calls ≤ x")
    ax.set_ylim(0, 1.02)
    ax.set_title("Routing cost per tool call (actor → tool function)")
    ax.legend(loc="lower right")
    ax.text(0, -0.24, "faasd: gateway → watchdog → handler.  Knative: Kourier → queue-proxy → watchdog → handler.",
            transform=ax.transAxes, fontsize=8, color=INK2)
    _save(fig, out, "fig5_route_ecdf")


def fig_sworkflow(stacks, out):
    parts = [("payload (data passed between steps)", "payload_bytes"),
             ("stored definitions", "definitions_bytes"), ("other metadata", "other_bytes")]
    rows = []
    for wl in WORKLOADS:
        for s in stacks:
            rs = [r for r in s.sw if r["workload"] == wl]
            if rs:
                rows.append((f"{s.label}\n{wl}", [statistics.fmean(_f(r[k]) for r in rs) / 1024 for _, k in parts],
                             statistics.fmean(_f(r["kb_per_turn"]) for r in rs)))
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(8, 0.75 * len(rows) + 1.4))
    for yi, (label, vals, per_turn) in enumerate(rows):
        left = 0.0
        for pi, v in enumerate(vals):
            ax.barh(yi, v, left=left, color=PART_COLORS[pi], edgecolor=SURFACE, linewidth=2, height=0.6,
                    label=parts[pi][0] if yi == 0 else None)
            if v >= 25:
                ax.text(left + v / 2, yi, f"{v:.0f}", ha="center", va="center", fontsize=8)
            left += v
        ax.text(left, yi, f"  {left:.0f} KB  ({per_turn:.0f} KB/turn)", va="center", fontsize=9)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows])
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("workflow state kept per run (KB of compact JSON)")
    ax.set_title("Sworkflow: orchestrator state per run", pad=46)
    ax.legend(ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0))
    _save(fig, out, "fig6_sworkflow")


def fig_rfriction(stacks, out):
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(9, 3.2), gridspec_kw={"width_ratios": [3, 1.3]})
    for si, s in enumerate(stacks):
        vals = s.run_vals("rfriction")
        if not vals:
            continue
        ax.scatter(vals, [si + _jitter(i % 7, 7, 0.3) for i in range(len(vals))], s=22, color=s.color,
                   alpha=0.75, edgecolors=SURFACE, linewidths=0.8, zorder=3)
        med = statistics.median(vals)
        ax.plot([med, med], [si - 0.28, si + 0.28], color=INK, lw=2, zorder=4)
        ax.text(med, si - 0.34, f"median {med:.2f}", ha="center", va="bottom", fontsize=9, bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.9))
        passed = len(s.runs) / s.total_runs if s.total_runs else 0
        ax2.barh(si, passed * 100, color=s.color, height=0.55)
        ax2.text(passed * 100, si, f" {passed:.0%} ({len(s.runs)}/{s.total_runs})", va="center", fontsize=9)
    ax.set_xscale("log")
    ax.set_yticks(range(len(stacks)), [s.label for s in stacks])
    ax.set_ylim(len(stacks) - 0.5, -0.7)
    ax.grid(axis="y", visible=False)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_xlabel("Rfriction = (Torch_run + ΣTroute) / T_LLM  (log scale)")
    ax.set_title("System friction per run")
    ax2.set_yticks(range(len(stacks)), [""] * len(stacks))
    ax2.set_ylim(len(stacks) - 0.5, -0.7)
    ax2.set_xlim(0, 135)
    ax2.grid(axis="y", visible=False)
    ax2.set_xlabel("runs completed with reward 1 (%)")
    ax2.set_title("Task success")
    notes = [f"{s.label}: reward inferred from the final answer (DB not re-checked)" for s in stacks
             if any(r.get("reward_verified") == "False" for r in s.runs)]
    if notes:
        fig.text(0.01, -0.06, "; ".join(notes), fontsize=8, color=INK2)
    _save(fig, out, "fig7_rfriction")


def fig_torch_by_turn(stacks, out):
    fig, axes = plt.subplots(1, len(stacks), figsize=(4.2 * len(stacks), 3.3), squeeze=False)
    for ax, s in zip(axes[0], stacks):
        for wi, wl in enumerate(WORKLOADS):
            by_turn = {}
            for t in s.turns:
                if t["workload"] == wl and _f(t.get("torch_ms")) is not None:
                    by_turn.setdefault(int(t["turn"]), []).append(_sec(_f(t["torch_ms"])))
            if not by_turn:
                continue
            ks = sorted(by_turn)
            meds = [statistics.median(by_turn[k]) for k in ks]
            style = "-" if wi == 0 else "--"
            ax.plot(ks, meds, style, color=s.color, lw=2, marker="o" if wi == 0 else "s", ms=8,
                    markeredgecolor=SURFACE, markeredgewidth=2, label=wl)
            for k in ks:
                ax.scatter([k] * len(by_turn[k]), by_turn[k], s=8, color=s.color, alpha=0.25, edgecolors="none")
        ax.set_title(s.label)
        ax.set_xlabel("loop turn")
        ax.set_ylabel("Torch in that turn (s)")
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper right")
    fig.suptitle("Does orchestration overhead grow as the agent's history grows?", x=0.01, ha="left",
                 fontweight="bold", fontsize=12, y=1.04)
    fig.text(0.01, -0.08, "Lines: median per turn; faint dots: individual runs. Each panel has its own y-scale.",
             fontsize=8, color=INK2)
    _save(fig, out, "fig8_torch_by_turn")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--conductor", nargs="+", required=True)
    p.add_argument("--argo", nargs="+", required=True, help="Argo run(s) at the default requeue time")
    p.add_argument("--argo-tuned", nargs="*", default=[], help="optional: Argo run(s) with a tuned requeue time")
    p.add_argument("--tuned-label", default="Argo + Knative (2 s requeue)")
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "plots"))
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    stacks = [Stack("Conductor + faasd", args.conductor, STACK_COLORS[0]),
              Stack("Argo + Knative", args.argo, STACK_COLORS[1])]
    if args.argo_tuned:
        stacks.append(Stack(args.tuned_label, args.argo_tuned, STACK_COLORS[2]))
    for s in stacks:
        print(f"{s.label}: {len(s.runs)}/{s.total_runs} runs usable, {len(s.turns)} turns, "
              f"{sum(1 for c in s.calls if c['kind'] == 'tool')} tool calls, {len(s.sw)} Sworkflow rows")

    fig_te2e(stacks, args.out)
    fig_breakdown(stacks, args.out)
    fig_torch_turn(stacks, args.out)
    fig_timeline(stacks, args.out)
    fig_route_ecdf(stacks, args.out)
    fig_sworkflow(stacks, args.out)
    fig_rfriction(stacks, args.out)
    fig_torch_by_turn(stacks, args.out)


if __name__ == "__main__":
    main()
