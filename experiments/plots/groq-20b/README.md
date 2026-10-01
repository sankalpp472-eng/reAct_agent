# Experiment 1 with a real LLM: all three configurations on Groq gpt-oss-20b

Same loop, functions and workloads as the mock-LLM runs; the mock is replaced by Groq's
`openai/gpt-oss-20b` (native tool calling), called from the cluster VM through the node's
SSH tunnel. Workloads: `retail-status` and `airline-status`, two short read-only tasks
made for this study on τ-bench's retail/airline data and tools (not τ-bench tasks).
5 runs per task per configuration, no warm-up, 60 s pause between runs.

| Configuration | Result folder | Runs with reward 1 |
|---|---|---|
| Conductor + faasd | `results/exp1-conductor-20261001-072147` | 10/10 |
| Argo + Knative, 2 s requeue | `results/exp1-argo-20261001-064730` | 9/10 (retail-status run 5 hit Groq's daily token limit) |
| Argo + Knative, 10 s requeue (Argo's default) | `results/exp1-argo-20261001-054740` | 10/10 |

All three use the same model, so every metric compares directly. Per-task medians are in
[`metrics.md`](metrics.md); all experiments in [`../ALL_RESULTS.md`](../ALL_RESULTS.md);
every run in [`../all_runs.csv`](../all_runs.csv).

## Medians per task

| | Conductor + faasd | Argo 2 s | Argo 10 s |
|---|---|---|---|
| **airline-status** (1 turn) | | | |
| Te2e | 5.9 s | 26.9 s | 69.3 s |
| T_LLM (rate-limit wait) | 3.9 s (0) | 3.7 s (0) | 3.9 s (0) |
| Torch_run | 1.7 s | 23.1 s | 65.6 s |
| Troute per tool call | 25.0 ms | 11.9 ms | 11.7 ms |
| Rfriction_net | 0.52 | 5.61 | 16.27 |
| **retail-status** (2 turns) | | | |
| Te2e | 69.1 s | 56.4 s | 130.6 s |
| T_LLM (rate-limit wait) | 66.7 s (58.0) | 19.1 s (9.0) | 8.7 s (0) |
| Torch per turn (between planner calls) | 0.91 s | 13.43 s | 57.58 s |
| Torch_run | 2.6 s | 35.1 s | 121.5 s |
| Troute per tool call | 22.6 ms | 11.3 ms | 11.8 ms |
| Rfriction_net | 0.33 | 4.29 | 14.04 |

## Findings

- **Orchestration:** Conductor adds ~1–3 s per run; Argo adds 23–35 s at a 2 s requeue
  and 66–122 s at its 10 s default. Per turn: 0.9 s vs 13.4 s vs 57.6 s.
- **Routing:** Knative routes a tool call in ~12 ms, faasd in ~23–25 ms, on every task.
- **Friction:** with Conductor, orchestration + routing is a third to a half of the model's
  own time (Rfriction_net 0.33–0.52); with Argo it is 4–6× (2 s) and 14–16× (10 s) the
  model's time. The 20b model is fast (~4 s of model time for airline-status), so the
  orchestrator's fixed cost dominates.
- **Rate limits only hit the fast stack.** Conductor issues the model calls back to back
  and hits Groq's per-minute token limit on retail-status (58 s of waiting, which is why its
  Te2e there exceeds Argo 2 s). Argo's slower turns spread the calls out. The waits are
  recorded separately and excluded from Rfriction_net; compare the stacks on Torch,
  Troute and Rfriction_net, not on Te2e.
- **Torch / turn in the figures is Torch_run / turns**, which includes workflow start and
  finish. For the 1-turn airline-status runs that is the whole run's orchestration.

Reproduce:
```bash
python3 experiments/plots_groq.py \
  --config "Conductor + faasd|gpt-oss-20b|experiments/results/exp1-conductor-20261001-072147" \
  --config "Argo + Knative, 2 s requeue|gpt-oss-20b|experiments/results/exp1-argo-20261001-064730" \
  --config "Argo + Knative, 10 s requeue|gpt-oss-20b|experiments/results/exp1-argo-20261001-054740" \
  --out experiments/plots/groq-20b
```
