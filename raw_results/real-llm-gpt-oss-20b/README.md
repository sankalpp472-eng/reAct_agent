# Raw results: Experiment 1 with a real LLM (Groq gpt-oss-20b)

All three configurations ran the same planner → actor → evaluator loop on the cluster VM
(node13), with Groq's `openai/gpt-oss-20b` as the LLM. Two read-only tasks made for this
study on τ-bench's retail/airline data and tools: `retail-status` and `airline-status`.
5 runs per task per configuration, no warm-up, 60 s pause between runs.

| Folder | Configuration | Copied from `experiments/results/` | Runs with reward 1 |
|---|---|---|---|
| `conductor-faasd/` | Conductor + faasd | `exp1-conductor-20261001-072147` | 10/10 |
| `argo-knative-2s-requeue/` | Argo + Knative, 2 s requeue | `exp1-argo-20261001-064730` | 9/10 (retail-status run 5 hit Groq's daily token limit) |
| `argo-knative-10s-requeue/` | Argo + Knative, 10 s requeue (Argo's default) | `exp1-argo-20261001-054740` | 10/10 |

## Files

Here, for all three configurations together:
- `runs.csv`: one row per run (30). `used` = scored reward 1 (counted in the medians).
  Times in ms: `te2e_ms`, `t_llm_ms` (incl. `t_llm_wait_ms`, Groq rate-limit waits),
  `torch_run_ms`, `troute_orch_ms`, `troute_tool_ms`; plus `rfriction`, `rfriction_net`.
- `tool_calls.csv`: one row per actor → tool call: `twarm_ms`, `troute_ms`.
- `metrics.md` / `metrics.csv`: medians per configuration and task.

In each configuration folder (as written by the experiment drivers):
- `raw/<task>-run<N>.json`: the full workflow record of each run (Conductor execution or
  Argo Workflow), including every function's response and its `_timing`. View one turn by
  turn with `python3 experiments/show_run.py <file>`.
- `dataplane_runs.csv`, `dataplane_turns.csv`, `dataplane_calls.csv`: metrics per run, per
  turn and per call, with the same definitions on both stacks (`experiments/dataplane.py`).
- `summary.json`: the driver's settings (model, Argo requeue time, runs) and statistics.
- Conductor only: `runs.csv`, `cycles.csv`, `calls.csv`, from Conductor's own task timestamps.

Figures for these runs: `experiments/plots/groq-20b/`. All experiments: `experiments/plots/ALL_RESULTS.md`.
