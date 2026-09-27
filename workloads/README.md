# Workloads (from tau-bench)

Two tasks taken from tau-bench's test sets (`tau_bench/envs/{retail,airline}/tasks_test.py`,
commit `59a200c`), one per domain. These are the actual benchmark tasks; tau-bench's
`few_shot_data/` is something else (example conversations used to prompt their
few-shot agent).

| Workload | Tool function | Expected tool calls | What it tests |
|---|---|---|---|
| [`retail-44`](retail-44.json) | `retail-tools` | `find_user_id_by_name_zip` → `get_order_details` → `get_product_details` → `calculate` → `modify_pending_order_items` | Swap a desk lamp in a pending order for the cheapest available one, refund the difference to a gift card, and report the amount (`17.99`). |
| [`airline-26`](airline-26.json) | `airline-tools` | `cancel_reservation` → `get_reservation_details` → `search_direct_flight` ×2 → `calculate` → `update_reservation_flights` | Cancel two reservations and upgrade a third to business class. **Policy trap:** `IFOYYZ` is basic economy, has no insurance and was booked more than 24h ago, so it must *not* be cancelled. Only `NQNU5R` is. |

## Why these two

- Each needs 5–6 tool calls, which fits the Conductor loop's 8-step cap.
- They cover most tool types in their domain: lookups, search, `calculate`, and a
  state-changing write. That means scoring checks what the agent *did*, not just what it said.
- `retail-44` has a numeric answer to check. `airline-26` checks whether the agent follows
  the domain policy (`policies/airline.md`) and doesn't just do what was asked.
- All the information is in the request up front. tau-bench normally runs a simulated
  user who reveals details over several turns; our planner → actor → evaluator loop
  gets a single `goal`, so tasks that depend on back-and-forth (e.g. "if the agent asks
  for confirmation, say no") were skipped.

## File format

- `goal`: what to send as the Conductor workflow's `goal` input. It's the tau-bench
  instruction rewritten from "You are X, you want…" into a request to the agent. It
  says the customer has already confirmed, since there's no simulated user to confirm
  a write.
- `original_instruction`: the unmodified tau-bench user instruction.
- `policy_file`: the domain's agent policy (tau-bench's `wiki.md`). tau-bench gives this
  to the agent as its system prompt.
- `expected_actions`, `expected_outputs`: the tau-bench ground truth.
- `expected_db_hash`: the tool server's `{"action":"hash"}` value after replaying
  `expected_actions` on a freshly reset DB (computed with the vendored tools).

## Running and scoring a workload

```bash
GW=http://<faasd-host>:8080
python workloads/score.py workloads/retail-44.json --gateway $GW --reset   # before the run
# ... run the workflow with goal = the workload's "goal" ...
python workloads/score.py workloads/retail-44.json --gateway $GW --answer "<final answer>"
```

`reward` is 1 only when the DB hash matches (the right writes happened and nothing else
changed) **and** every `expected_outputs` string appears in the answer. This is the same
rule tau-bench uses.
