# Experiment 1 with a real LLM (Groq)

The same planner → actor → evaluator loop and functions as the mock-LLM runs, with the
mock replaced by Groq's hosted `gpt-oss` models (OpenAI-compatible API, native tool
calling). Run on the cluster VM; the functions reach Groq through the node's SSH tunnel.

- **Workloads:** `retail-status` and `airline-status`, two short read-only tasks made for
  this study on τ-bench's retail/airline data, tools and scoring (not τ-bench tasks).
  They need ~6–10 model calls per run, so they fit Groq's free-tier limits.
- **Runs:** 5 per task per configuration, no warm-up, 60 s pause between runs.
  All 30 runs scored reward 1.

| Configuration | Model | Result folder |
|---|---|---|
| Conductor + faasd | gpt-oss-120b | `results/exp1-conductor-20260930-173536` |
| Argo + Knative, 2 s requeue | gpt-oss-120b | `results/exp1-argo-20260930-180520` |
| Argo + Knative, 10 s requeue (Argo's default) | **gpt-oss-20b** | `results/exp1-argo-20261001-054740` |

The 10 s configuration ran with the smaller model because gpt-oss-120b's free daily token
allowance was used up. **Torch, Troute and Twarm don't depend on the model**, so all three
configurations compare directly on them. T_LLM and Rfriction do depend on it, so for those
compare Conductor with Argo 2 s (same model) only.

## Metrics (medians)

See [`metrics.md`](metrics.md) (per workload too) and `metrics.csv`.

| | Conductor + faasd | Argo 2 s | Argo 10 s |
|---|---|---|---|
| Torch per turn (Torch_run / turns) | **1.6 s** | 18.5 s | 60.9 s |
| Troute per tool call | 22.7 ms | **11.6 ms** | 11.8 ms |
| Twarm per tool call | 0.12 ms | 0.12 ms | 0.13 ms |
| Te2e | 49.9 s | 61.9 s | 100.2 s |
| T_LLM (of which rate-limit wait) | 45.9 s (33.5 s) | 32.4 s (23.0 s) | 5.8 s (0 s) |
| Rfriction_net | **0.35** | 3.88 | 15.1 *(20b)* |

## Figures

| Figure | What it shows |
|---|---|
| `figG1_breakdown` | Mean Te2e split into model time, Groq rate-limit waits (hatched), Torch, Troute and the functions' own work |
| `figG2_torch_per_turn` | Orchestration overhead per agent turn, per run (log scale) |
| `figG3_troute_tool` | Routing overhead per actor → tool call |
| `figG4_rfriction_net` | (Torch + ΣTroute) relative to the model's own time, without rate-limit waits |

## Findings

- **The mock-LLM conclusions hold with a real model.** Argo's orchestration overhead per
  turn is ~11× Conductor's with a 2 s requeue and ~38× with Argo's 10 s default, because
  Argo advances a workflow on its requeue cycle while Conductor reacts to task completion.
- **Knative routes a tool call about twice as fast as faasd** (11.6 vs 22.7 ms),
  independent of requeue setting and model.
- **With Conductor, orchestration is small next to the LLM** (Rfriction_net 0.35). With
  Argo it outweighs the LLM: 3.9× at 2 s, and ~15× at 10 s with the faster 20b model.
- **Rate limits dominate Te2e on the free tier** (Conductor: 33.5 of 49.9 s). Waits are
  recorded separately (`_timing.llm_wait_ms`) and left out of Rfriction_net, so they don't
  bias the stack comparison. The slow Argo 10 s runs spread model calls out enough to
  avoid waits entirely, which is why Te2e alone is not a fair comparison here.
- **Torch per turn here is Torch_run / turns**, so it includes the workflow's start and
  finish (pod scheduling on Argo); the per-turn values in `dataplane_turns.csv` are lower
  (e.g. ~12 s at 2 s requeue) but exist only between two planner calls, i.e. not for
  1-turn runs.

Reproduce:
```bash
python3 experiments/plots_groq.py \
  --config "Conductor + faasd|gpt-oss-120b|experiments/results/exp1-conductor-20260930-173536" \
  --config "Argo + Knative, 2 s requeue|gpt-oss-120b|experiments/results/exp1-argo-20260930-180520" \
  --config "Argo + Knative, 10 s requeue|gpt-oss-20b|experiments/results/exp1-argo-20261001-054740" \
  --out experiments/plots/groq
```
