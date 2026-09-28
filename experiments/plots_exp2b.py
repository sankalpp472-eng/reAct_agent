"""
Figures for Experiment 2b (cold tool function, through the orchestrator),
from exp2b_cold.py result folders.

  python experiments/plots_exp2b.py \\
      --conductor experiments/results/exp2b-conductor-20260928-104555 \\
      --argo      experiments/results/exp2b-argo-20260928-115146 \\
      --out experiments/plots

Each folder needs trials.csv and raw/ (the warm run's per-call handler times
come from raw/). Several folders per stack are merged. Only trials where both
runs scored reward 1 and the first tool call succeeded are plotted. Same
palette and style as plots.py; writes PNG + PDF per figure.
"""
import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plots import INK, INK2, PART_COLORS, STACK_COLORS, SURFACE, WORKLOADS, _f, _jitter, _read, _save, plt  # noqa: E402


def _tool_calls(calls):
    return [tc for c in calls for tc in c.get("tool_calls", [])]


class Stack:
    def __init__(self, label, folders, color, calls_fn):
        self.label, self.color = label, color
        rows = _read(folders, "trials.csv")
        self.total = len(rows)
        self.trials = [r for r in rows if _f(r["reward_cold"]) == 1.0 and _f(r["reward_warm"]) == 1.0
                       and r["first_call_error"] == "False" and _f(r["tcold_tool_ms"]) is not None]
        for r in self.trials:
            # median handler time (t4 - t3) of the warm run's tool calls: the
            # part of the cold handler time above it is app-level init
            with open(os.path.join(r["_folder"], "raw", f"{r['workload']}-trial{r['trial']}.json")) as f:
                warm = json.load(f)["warm"]
            tw = [tc["twarm_ms"] for tc in _tool_calls(calls_fn(warm)) if "twarm_ms" in tc]
            r["warm_handler_ms"] = statistics.median(tw) if tw else 0.0

    def vals(self, key, workload=None):
        return [_f(r[key]) for r in self.trials if workload in (None, r["workload"])]


def _conductor_calls(wf):
    from exp1_conductor import dataplane_calls
    return dataplane_calls(wf)


def _argo_calls(wf):
    from exp1_argo import extract_calls
    return extract_calls(wf)[0]


# ---- figures ------------------------------------------------------------------

def fig_tcold(stacks, out):
    fig, ax = plt.subplots(figsize=(7, 3.6))
    w = 0.8 / len(stacks)
    for si, s in enumerate(stacks):
        for wi, wl in enumerate(WORKLOADS):
            vals = [v / 1000 for v in s.vals("tcold_tool_ms", wl)]
            if not vals:
                continue
            x = wi + (si - (len(stacks) - 1) / 2) * w
            ax.scatter([x + _jitter(i, len(vals), w * 0.6) for i in range(len(vals))], vals,
                       s=26, color=s.color, alpha=0.8, edgecolors=SURFACE, linewidths=0.8,
                       label=s.label if wi == 0 else None, zorder=3)
            med = statistics.median(vals)
            ax.plot([x - w * 0.38, x + w * 0.38], [med, med], color=INK, lw=2, zorder=4)
            ax.annotate(f"{med:.2f} s", (x + w * 0.4, med), xytext=(3, 0), textcoords="offset points",
                        va="center", fontsize=9, color=INK)
    ax.set_xticks(range(len(WORKLOADS)), WORKLOADS)
    ax.set_ylabel("Tcold (s)")
    ax.set_ylim(bottom=0)
    ax.set_title("Cold-start penalty of the first tool call")
    ax.legend(loc="lower left")
    ax.text(0, -0.2, "Tcold = T_first_invocation − Twarm, on the actor's clock. Dots: trials. Bars: median.",
            transform=ax.transAxes, fontsize=8, color=INK2)
    _save(fig, out, "fig9_tcold")


def fig_breakdown(stacks, out):
    """Median first call to the cold tool, split into its parts."""
    parts = ["warm round trip (Twarm)", "app init (DB load in the handler)", "platform start"]
    fig, ax = plt.subplots(figsize=(7, 2.6))
    for si, s in enumerate(stacks):
        twarm = statistics.median(s.vals("warm_tool_http_median_ms"))
        init = statistics.median([max(_f(r["cold_handler_ms"]) - r["warm_handler_ms"], 0.0) for r in s.trials])
        total = statistics.median(s.vals("cold_first_call_http_ms"))
        segs = [twarm, init, max(total - twarm - init, 0.0)]
        left = 0.0
        for pi, v in enumerate(segs):
            ax.barh(si, v / 1000, left=left / 1000, height=0.5, color=PART_COLORS[pi], edgecolor=SURFACE,
                    linewidth=2, label=parts[pi] if si == 0 else None)
            if v / total > 0.08:
                ax.text((left + v / 2) / 1000, si, f"{v / 1000:.2f} s", ha="center", va="center",
                        fontsize=8.5, color="#ffffff")
            left += v
        ax.annotate(f"{total / 1000:.2f} s", (total / 1000, si), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK)
    ax.set_yticks(range(len(stacks)), [s.label for s in stacks])
    ax.invert_yaxis()
    ax.set_xlabel("first call to the cold tool, T_first_invocation (s)")
    ax.set_xlim(0, ax.get_xlim()[1] * 1.08)
    ax.grid(axis="y", visible=False)
    ax.set_title("What the first call to a cold tool spends its time on (medians)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=3)
    _save(fig, out, "fig10_tcold_breakdown")


def fig_dte2e(stacks, out):
    fig, ax = plt.subplots(figsize=(5.2, 4))
    lo, hi = 0.0, 0.0
    for s in stacks:
        x = [v / 1000 for v in s.vals("tcold_tool_ms")]
        y = [v / 1000 for v in s.vals("delta_te2e_ms")]
        ax.scatter(x, y, s=30, color=s.color, alpha=0.85, edgecolors=SURFACE, linewidths=0.8,
                   label=s.label, zorder=3)
        lo, hi = min(lo, *y), max(hi, *x, *y)
    ax.plot([0, hi * 1.05], [0, hi * 1.05], color=INK2, lw=1, ls="--", zorder=2)
    ax.annotate("ΔTe2e = Tcold", (hi * 0.62, hi * 0.62), xytext=(4, -12), textcoords="offset points",
                fontsize=8, color=INK2)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_xlim(0, hi * 1.05)
    ax.set_ylim(lo * 1.1, hi * 1.1)
    ax.set_xlabel("Tcold of the first tool call (s)")
    ax.set_ylabel("ΔTe2e = Te2e(cold) − Te2e(warm) (s)")
    ax.set_title("End-to-end cost vs. cold start, per trial")
    ax.legend(loc="lower right")
    ax.text(0, -0.2, "On Argo, run-to-run scheduling noise is larger than the cold start itself.",
            transform=ax.transAxes, fontsize=8, color=INK2)
    _save(fig, out, "fig11_dte2e_vs_tcold")


def fig_attempts(stacks, out):
    fig, ax = plt.subplots(figsize=(6, 2.6))
    for si, s in enumerate(stacks):
        vals = sorted(s.vals("first_call_attempts"))
        ax.scatter(vals, [si + _jitter(i, len(vals), 0.3) for i in range(len(vals))], s=30, color=s.color,
                   alpha=0.85, edgecolors=SURFACE, linewidths=0.8, zorder=3)
        ax.annotate(f"median {statistics.median(vals):.0f}", (max(vals), si), xytext=(8, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK)
    ax.set_yticks(range(len(stacks)), [s.label for s in stacks])
    ax.invert_yaxis()
    ax.set_ylim(len(stacks) - 0.5, -0.5)
    ax.set_xlim(0, ax.get_xlim()[1] + 2)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("requests the actor sent before the cold tool answered")
    ax.set_title("Requests needed for the first call to a cold tool")
    ax.text(0, -0.38, "Retried every 50 ms while the tool can't answer yet (TOOL_COLD_RETRY_S). "
            "1 = answered on the first request.", transform=ax.transAxes, fontsize=8, color=INK2)
    _save(fig, out, "fig12_first_call_attempts")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--conductor", nargs="+", required=True, help="exp2b-conductor-* folder(s)")
    p.add_argument("--argo", nargs="+", required=True, help="exp2b-argo-* folder(s)")
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "plots"))
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    stacks = [Stack("Conductor + faasd", args.conductor, STACK_COLORS[0], _conductor_calls),
              Stack("Argo + Knative", args.argo, STACK_COLORS[1], _argo_calls)]
    for s in stacks:
        print(f"{s.label}: {len(s.trials)}/{s.total} trials used")
    fig_tcold(stacks, args.out)
    fig_breakdown(stacks, args.out)
    fig_dte2e(stacks, args.out)
    fig_attempts(stacks, args.out)


if __name__ == "__main__":
    main()
