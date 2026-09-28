# Orchestrating an agentic loop on serverless: Conductor + faasd vs Argo + Knative

A study comparing two orchestrator + FaaS stacks running the **same** agent loop:

| | Stack A (lighter) | Stack B (heavier) |
|---|---|---|
| Orchestrator | Conductor OSS: JSON workflow, `DO_WHILE` loop of HTTP + JQ tasks | Argo Workflows 3.6: recursive steps, HTTP templates run by an agent pod |
| FaaS | faasd (OpenFaaS): gateway → watchdog → function | Knative Serving 1.19 + Kourier: Kourier → queue-proxy → function |
| Runs on | containerd, no Kubernetes | k3s 1.33 |

Only the orchestrator and the FaaS platform change. Both stacks use the same function
images, the same loop, the same workloads and the same measurement code.

## The agent

A plan → act → evaluate loop, at most 8 turns, built from five functions:

| Function | Role |
|---|---|
| `planner` | Takes the goal (plus the history so far) and returns the remaining steps |
| `actor` | Executes one step by calling the domain's tools |
| `evaluator` | Looks at the plan and history, and decides `done` / `continue` / `replan` |
| `retail-tools`, `airline-tools` | The [τ-bench](https://github.com/sierra-research/tau-bench) tools (commit `59a200c`, MIT, see `retail-tools/LICENSE-tau-bench`), one function per domain, over an in-memory mock database |

**Workloads.** The agent solves two τ-bench tasks ([`workloads/`](workloads/README.md)):
- **retail-44** (5 turns): swap a desk lamp in a pending order for the cheapest one available, refund the difference to the gift card and report it ($17.99).
- **airline-26** (6 turns): cancel two reservations and upgrade a third to business class, following the airline policy, which forbids cancelling one of them.

Each run is scored as in τ-bench: the database end state and the expected answer.

**Mock LLM.** The planner, actor and evaluator use a mock of the Gemini client
(`gemini_client.py` + `mock_workloads.py`). It replays each workload's scripted steps and
sleeps to stand in for real API latency: 2 s / 2 s / 1 s per planner / actor / evaluator
call (`MOCK_LATENCY_S`). The actor's tool calls are real calls to the tool functions, so
the database really changes and runs can be scored.
- This study measures orchestration and FaaS overhead. LLM quality and fine-tuning are
  out of scope, so the testbed is effectively a simulator.
- Every run does identical work, so differences in latency come from the stack.

## Repository layout

| Path | What |
|---|---|
| `planner/`, `actor/`, `evaluator/` | Agent functions (OpenFaaS `python3-http` template). `gemini_client.py`, `mock_workloads.py` and `telemetry.py` must stay identical in all of them |
| `retail-tools/`, `airline-tools/` | τ-bench tool functions (`tau_tools/`, `data/*.json` vendored from τ-bench) |
| `stack.yaml` | faasd deployment of all five functions |
| `conductor-agentic-loop/` | The Conductor workflow `pae_agentic_loop` |
| `argo-knative/` | Stack B: install, Knative Services, Argo WorkflowTemplate. See its [README](argo-knative/README.md) |
| `workloads/` | The two τ-bench tasks, scoring (`score.py`), and a local runner without an orchestrator (`run_local.py`) |
| `experiments/` | Experiment drivers, metrics and plots. See its [README](experiments/README.md) |
| `experiments/results/` | Raw and summarized results of every run |
| `experiments/plots/` | Figures (PNG + PDF). See its [README](experiments/plots/README.md) |

## Running Stack A: Conductor + faasd

Prerequisites: faasd running, `faas-cli`, Docker to build, a registry faasd can pull from,
and Conductor OSS.

```bash
faas-cli template store pull python3-http          # once; adds template/

# The functions declare these secrets. The mock never reads them, so placeholders are fine
faas-cli secret create gemini-api-key --from-literal=unused
faas-cli secret create tavily-api-key --from-literal=unused

# Change the image names in stack.yaml to your registry, then build + push + deploy
faas-cli up -f stack.yaml

# Register the workflow (the HTTP tasks call the faasd gateway at 172.17.0.1:8080)
CONDUCTOR=http://localhost:8082/api
jq -s '.' conductor-agentic-loop/pae_agentic_loop_v2.json \
  | curl -s -X PUT "$CONDUCTOR/metadata/workflow" -H 'Content-Type: application/json' -d @-
```

Start a run with the workload's goal and domain:
```bash
curl -s "$CONDUCTOR/workflow/pae_agentic_loop" -H 'Content-Type: application/json' \
  -d "{\"goal\": $(jq .goal workloads/retail-44.json), \"domain\": \"retail\", \"context\": \"\"}"
```
The workflow returns `final_answer` and `step_history` as outputs. Reset the tool
database before each run (`{"action":"reset"}`, below).

## Running Stack B: Argo Workflows + Knative

The same images run as Knative Services, and the loop runs as an Argo WorkflowTemplate.
Install, deploy and troubleshooting steps are in [`argo-knative/README.md`](argo-knative/README.md).
Run only one stack at a time.

## Experiments and results

Each function adds a `_timing` object to its response: handler entry/exit, LLM time,
and for the actor, each tool call's send/receive times and attempts. The drivers combine
these with their own clocks, so both stacks are measured by the same code
([`experiments/README.md`](experiments/README.md)).

**Experiment 1: warm baseline** (`exp1_conductor.py`, `exp1_argo.py`; figures 1–8).
Medians, Conductor + faasd vs Argo + Knative with Argo's default 10 s requeue:

| Metric | Conductor + faasd | Argo + Knative |
|---|---|---|
| Te2e, retail-44 / airline-26 | 53 s / 64 s | 294 s / 337 s |
| Orchestration per loop turn (Torch) | 0.75 s | 41 s |
| Actor → tool routing (Troute) | 20 ms | 127 ms |
| Rfriction = overhead / LLM time | 0.11 | 4.97 |
| Workflow state per run (Sworkflow), retail-44 / airline-26 | 293 KB / 513 KB | 89 KB / 163 KB |
| Task success | 100% | 100% |

**Experiment 2b: cold tool function, through the orchestrator** (`exp2b_cold.py`;
figures 9–12). The tool function is made dormant, then the workflow runs. Medians,
retail-44 / airline-26:

| Metric | Conductor + faasd | Argo + Knative |
|---|---|---|
| Tcold = T_first_invocation − Twarm | 1.72 s / 1.81 s | 2.91 s / 3.04 s |
| Of which platform start (overall) | 1.69 s: new process in the existing container | 2.80 s: new pod |

Without a retry, faasd's first call to a cold function fails, because its gateway
forwards before the function is listening. The agent then needs an extra turn (+11 s).
The actor therefore retries such calls (`TOOL_COLD_RETRY_S`). Knative's activator holds
the request instead.

Findings and caveats for each figure are in [`experiments/plots/README.md`](experiments/plots/README.md).

**Still to do:** Experiment 1 on Argo with the tuned 2 s requeue, and Experiment 3
(scaling time Tscale, control-plane CPU/RAM).

## Function contracts

| Function | Input | Output |
|---|---|---|
| planner | `{goal, context?, feedback?}` | `{goal, plan: [{id, description}]}` |
| actor | `{goal, plan, step_id, history?, domain?}` | `{step_id, description, result, status}` |
| evaluator | `{goal, plan, history}` | `{verdict: done\|continue\|replan, feedback, next_step_id}` |
| retail-tools / airline-tools | `{tool, arguments}` or `{action: list_tools\|reset\|hash}` | `{tool, output, error}` / `{tools}` / `{status}` / `{hash}` |

Every response also carries `_timing`. The mock picks a workload's script when the goal
contains that workload's key strings (see `mock_workloads.py`); any other goal gets a
generic plan. With `"domain": "retail"` or `"airline"`, the actor uses that domain's tool
function, reached through `TOOLS_GATEWAY_URL` on faasd or `TOOLS_URL_TEMPLATE` on Knative.

Tool outputs are strings, as in τ-bench. A tool-level failure comes back as HTTP 200
with `"error": true`, so the agent can react to it. Only malformed requests get a 4xx.

### Trying the functions by hand (faasd)

```bash
GW=http://127.0.0.1:8080
GOAL=$(jq -r .goal workloads/retail-44.json)

curl -s $GW/function/retail-tools -d '{"action":"reset"}'
curl -s $GW/function/retail-tools -d '{"tool":"find_user_id_by_name_zip","arguments":{"first_name":"Aarav","last_name":"Anderson","zip":"19031"}}' | jq
curl -s $GW/function/retail-tools -d '{"action":"hash"}' | jq

curl -s $GW/function/planner -H 'Content-Type: application/json' \
  -d "$(jq -n --arg g "$GOAL" '{goal: $g, context: ""}')" | jq

curl -s $GW/function/actor -H 'Content-Type: application/json' -d "$(jq -n --arg g "$GOAL" '{
  goal: $g, domain: "retail", step_id: 1, history: [],
  plan: [{id: 1, description: "Authenticate the customer: look up the user id for Aarav Anderson, zip 19031"}]}')" \
  | jq '{status, result, tool_calls: ._timing.tool_calls}'

curl -s $GW/function/evaluator -H 'Content-Type: application/json' -d "$(jq -n --arg g "$GOAL" '{
  goal: $g, plan: [{id: 1, description: "Authenticate"}, {id: 2, description: "Get the order"}],
  history: [{step_id: 1, status: "completed", result: "Authenticated."}]}')" | jq
```

To run a workload end to end without an orchestrator (resets the DB, runs the loop,
scores the result):
```bash
MOCK_LATENCY_S=0 python workloads/run_local.py workloads/retail-44.json            # all in-process
python workloads/run_local.py workloads/retail-44.json --gateway http://127.0.0.1:8080
```

## Using a real LLM

`gemini_client.py` keeps the real client's function signatures (`call_gemini_json`,
`call_gemini_agentic`). To run with Gemini, swap the mock for a real client, then create
the `gemini-api-key` secret with a real key. The key is mounted at
`/var/openfaas/secrets/gemini-api-key`; never put it in `stack.yaml` or the image.
Expect longer and more variable LLM times, which would change Rfriction but not the
orchestration and routing overheads.
