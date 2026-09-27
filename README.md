# Planner / Actor / Evaluator — faasd functions (Gemini-powered)

Three OpenFaaS functions, meant to run on **faasd**, each calling the Gemini API:

- `planner`   — takes a goal, returns a step-by-step plan (JSON)
- `actor`     — executes one step of the plan, returns a result (JSON)
- `evaluator` — looks at the plan + history of results, decides `done` / `continue` / `replan`

They're plain HTTP JSON functions (no orchestration logic inside them) so you can
drive the loop from Conductor, a shell script, or anything else that can make
HTTP calls to the faasd gateway.

## 0. Prerequisites (on the faasd host)

- faasd already installed and running (`sudo systemctl status faasd`)
- `faas-cli` installed on the machine you deploy from:
  ```bash
  curl -sSL https://cli.openfaas.com | sudo sh
  ```
- The `python3-http` template pulled into this project:
  ```bash
  cd pae-faasd
  faas-cli template store pull python3-http
  ```
  This adds a `template/` folder that `stack.yaml` needs at build time.

- Docker (or Podman) available for `faas-cli build`, and a container registry
  you can push to (Docker Hub, GHCR, a self-hosted registry, etc.) — faasd
  pulls images, it doesn't build them for you.

## 1. Get a Gemini API key

Create one at https://aistudio.google.com/apikey.

## 2. Log in to faasd and create the secret

```bash
# Get the faasd gateway password (on the faasd host)
sudo cat /var/lib/faasd/secrets/basic-auth-password

# From your deploy machine:
export OPENFAAS_URL=http://<faasd-host>:8080
faas-cli login --username admin --password <the-password-above>

# Store the Gemini key as an OpenFaaS secret (never put it in stack.yaml directly)
faas-cli secret create gemini-api-key --from-literal="<YOUR_GEMINI_API_KEY>"
```

## 3. Update image names

In `stack.yaml`, change `pae/planner:latest` etc. to `<your-registry-username>/planner:latest`
(and same for actor/evaluator) so `faas-cli` can push them somewhere faasd can pull from.

## 4. Build, push, deploy

```bash
cd pae-faasd
faas-cli up -f stack.yaml
```
`up` = build + push + deploy in one shot. You can also run the three steps separately:
```bash
faas-cli build   -f stack.yaml
faas-cli push    -f stack.yaml
faas-cli deploy  -f stack.yaml
```

## 5. Test each function

```bash
# Planner
curl -s http://<faasd-host>:8080/function/planner \
  -H 'Content-Type: application/json' \
  -d '{"goal": "Plan a 3-day trip to Lisbon under $800"}' | jq

# Actor  (use a step id from the planner's output, e.g. 1)
curl -s http://<faasd-host>:8080/function/actor \
  -H 'Content-Type: application/json' \
  -d '{
        "goal": "Plan a 3-day trip to Lisbon under $800",
        "plan": [{"id":1,"description":"Find flight options under $300"}],
        "step_id": 1,
        "history": []
      }' | jq

# Evaluator
curl -s http://<faasd-host>:8080/function/evaluator \
  -H 'Content-Type: application/json' \
  -d '{
        "goal": "Plan a 3-day trip to Lisbon under $800",
        "plan": [{"id":1,"description":"Find flight options under $300"}],
        "history": [{"step_id":1,"result":"Found a $260 round trip","status":"completed"}]
      }' | jq
```

## Contract summary (for wiring into Conductor later)

| Function  | Input                                              | Output                                                           |
|-----------|-----------------------------------------------------|--------------------------------------------------------------------|
| planner   | `{goal, context?, feedback?}`                       | `{goal, plan:[{id, description}]}`                                |
| actor     | `{goal, plan, step_id, history?}`                   | `{step_id, description, result, status}`                          |
| evaluator | `{goal, plan, history}`                             | `{verdict: done|continue|replan, feedback, next_step_id}`         |

The typical loop: `planner` → loop( `actor` on `next_step_id` → append to `history` →
`evaluator` ) until `verdict == "done"`; if `verdict == "replan"`, call `planner`
again with `feedback` and reset `history`. That loop-and-branch logic is exactly
what we'll model as a Conductor workflow next.

## Notes / things to harden before production

- Gemini calls have no retry/backoff here — add one if you hit rate limits.
- `call_gemini_json` assumes the model returns valid JSON (enforced via
  `response_mime_type: application/json`); still wrap with your own
  validation if you tighten the schema further.
- Secrets are read from `/var/openfaas/secrets/gemini-api-key` (mounted by
  the `secrets:` block in `stack.yaml`) — don't bake the key into the image.
- Consider setting `read_timeout` / `write_timeout` / `exec_timeout` in
  `stack.yaml` if Gemini responses are slow (LLM calls can exceed OpenFaaS's
  default 5s timeout).
