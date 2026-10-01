# Raw results

All measurements of the study in spreadsheet form, plus the main plots. Every row is
labelled with its dataset, stack configuration and task.

## Tables

`results.xlsx` has all five tables as sheets; the same tables are also here as CSV.

| File (sheet) | One row per | Contents |
|---|---|---|
| `medians_per_stack_and_task.csv` (Medians (Exp 1)) | dataset × stack × task | medians of every Exp 1 metric over the runs with reward 1 |
| `runs.csv` (Runs (Exp 1)) | run | Te2e, T_LLM, rate-limit wait, Torch, Troute, Rfriction, Sworkflow; `used` = reward 1 |
| `tool_calls.csv` (Tool calls (Exp 1)) | actor → tool call | Twarm, Troute |
| `cold_start_medians.csv` (Cold start medians) | machine × stack × task | Tcold, first/warm call time, attempts, ΔTe2e |
| `cold_start_trials.csv` (Cold start trials) | Exp 2b trial | every cold-start measurement |

Datasets (`dataset` column):
- **Mock LLM, laptop (WSL2)** and **Mock LLM, cluster (node13 VM)**: τ-bench tasks retail-44 and
  airline-26; the LLM is mocked (2 s / 2 s / 1 s per planner / actor / evaluator call).
- **Real LLM (Groq gpt-oss-120b)** and **Real LLM (Groq gpt-oss-20b)**: tasks retail-status
  and airline-status (read-only, on τ-bench's data). The 20b set covers all three stacks;
  the 120b set is partial (Argo 10 s airline-status hit Groq's daily token limit).

Stacks: Conductor + faasd; Argo + Knative with a 2 s requeue; Argo + Knative with Argo's
default 10 s requeue.

## Plots

| Folder | Experiment |
|---|---|
| `plots/mock-llm-cluster/` | Exp 1, mock LLM, cluster: Te2e, time breakdown, Torch per turn, timeline, routing, Sworkflow, Rfriction (fig1–8) |
| `plots/real-llm-gpt-oss-20b/` | Exp 1, real LLM (gpt-oss-20b), all three stacks, per task (figG1–G4) |
| `plots/cold-start-cluster/` | Exp 2b, cold tool function, cluster: Tcold, its breakdown, ΔTe2e, attempts (fig9–12) |

## Metrics

- **Te2e**: end-to-end time of a run. **T_LLM**: time in LLM calls (real LLM: includes
  Groq rate-limit waits, also given separately).
- **Torch**: orchestration overhead (per turn, or `torch_run` for the whole run).
- **Troute**: routing overhead of a call (orchestrator → function, actor → tool).
  **Twarm**: a warm tool function's own handler time.
- **Rfriction** = (Torch + ΣTroute) / T_LLM; **Rfriction_net** leaves rate-limit waits out.
- **Tcold** = first call to a cold tool − its warm round trip.

Source data and scripts: `experiments/results/` (raw run records) and
`experiments/all_results.py`, `experiments/plots*.py` (regenerate everything).
