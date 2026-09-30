"""
Scripted trajectories for the mock Gemini client, one per workload in
workloads/*.json. Keep this file identical in planner/, actor/ and evaluator/
(like gemini_client.py - each faasd function is built from its own directory).

When a goal matches a workload (all of its `match` strings appear in the goal),
the mock plays that workload's script instead of its generic plan:

  planner   -> returns the script steps not yet completed (global ids 1..N)
  actor     -> makes the step's tool calls for real, through the actor's
               execute_tool_fn (i.e. against the retail-tools / airline-tools
               faasd function), and reports the step's result plus the
               tool outputs
  evaluator -> "continue" to the next pending step, "replan" if the last
               step failed, "done" with `final_answer` once every step is done

The tool calls are tau-bench's ground-truth actions for the task, plus a few
read-only lookups a real agent would make (they don't change the DB, so the
end-state hash still matches `expected_db_hash`).
"""

WORKLOADS = [
    {
        "id": "retail-44",
        "domain": "retail",
        "match": ["#W9300146", "Desk Lamp"],
        "steps": [
            {
                "description": "Authenticate the customer: look up the user id for Aarav Anderson, zip 19031",
                "calls": [
                    ("find_user_id_by_name_zip",
                     {"first_name": "Aarav", "last_name": "Anderson", "zip": "19031"}),
                ],
                "result": "Customer authenticated as user aarav_anderson_8794.",
            },
            {
                "description": "Get order #W9300146 and confirm it is pending and contains the Desk Lamp",
                "calls": [
                    ("get_order_details", {"order_id": "#W9300146"}),
                ],
                "result": (
                    "Order #W9300146 is pending. It contains Desk Lamp item 9190635437 "
                    "(product 6817146515) at $153.23, paid with gift_card_7245904."
                ),
            },
            {
                "description": "Find the cheapest available Desk Lamp variant (product 6817146515)",
                "calls": [
                    ("get_product_details", {"product_id": "6817146515"}),
                ],
                "result": (
                    "Cheapest available Desk Lamp is item 5320792178 (black, medium "
                    "brightness, AC adapter) at $135.24."
                ),
            },
            {
                "description": "Calculate the price difference between the new and the current lamp",
                "calls": [
                    ("calculate", {"expression": "135.24 - 153.23"}),
                ],
                "result": "Price difference is -17.99, so the customer gets $17.99 back.",
            },
            {
                "description": (
                    "Swap item 9190635437 for 5320792178 in order #W9300146, "
                    "refunding the difference to the customer's gift card"
                ),
                "calls": [
                    ("get_user_details", {"user_id": "aarav_anderson_8794"}),
                    ("modify_pending_order_items", {
                        "order_id": "#W9300146",
                        "item_ids": ["9190635437"],
                        "new_item_ids": ["5320792178"],
                        "payment_method_id": "gift_card_7245904",
                    }),
                ],
                "result": (
                    "Order #W9300146 now contains Desk Lamp 5320792178. The $17.99 "
                    "difference was refunded to gift_card_7245904."
                ),
            },
        ],
        "final_answer": (
            "The Desk Lamp in order #W9300146 has been changed to the cheapest available "
            "one (black, medium brightness, AC adapter, $135.24). You get back $17.99 in "
            "total, refunded to your gift card."
        ),
    },
    {
        "id": "airline-26",
        "domain": "airline",
        "match": ["aarav_ahmed_6699", "M20IZO"],
        "steps": [
            {
                "description": "Get the profile of user aarav_ahmed_6699 to find the credit card ending in 7334",
                "calls": [
                    ("get_user_details", {"user_id": "aarav_ahmed_6699"}),
                ],
                "result": "Card ending in 7334 is credit_card_9074831 (mastercard).",
            },
            {
                "description": "Check whether reservation IFOYYZ can be cancelled under the cancellation policy",
                "calls": [
                    ("get_reservation_details", {"reservation_id": "IFOYYZ"}),
                ],
                "result": (
                    "IFOYYZ cannot be cancelled: it is basic economy without travel "
                    "insurance and was booked on 2024-05-12, more than 24 hours ago. "
                    "Not cancelling it."
                ),
            },
            {
                "description": "Cancel reservation NQNU5R (business class, which the policy allows)",
                "calls": [
                    ("cancel_reservation", {"reservation_id": "NQNU5R"}),
                ],
                "result": "Reservation NQNU5R cancelled; refund goes to the original payment method.",
            },
            {
                "description": "Get reservation M20IZO and look up business fares for its flights",
                "calls": [
                    ("get_reservation_details", {"reservation_id": "M20IZO"}),
                    ("search_direct_flight", {"origin": "JFK", "destination": "ATL", "date": "2024-05-22"}),
                    ("search_direct_flight", {"origin": "ATL", "destination": "MCO", "date": "2024-05-22"}),
                ],
                "result": (
                    "M20IZO is economy JFK->ATL (HAT268, $136) and ATL->MCO (HAT010, $109) "
                    "on 2024-05-22 for 2 passengers. Business fares: HAT268 $430, HAT010 $412."
                ),
            },
            {
                "description": "Calculate the upgrade cost for both passengers",
                "calls": [
                    ("calculate", {"expression": "(430 + 412 - (136 + 109)) * 2"}),
                ],
                "result": "Upgrade costs $597 per passenger, $1194 in total.",
            },
            {
                "description": "Upgrade M20IZO to business class, charging credit_card_9074831",
                "calls": [
                    ("update_reservation_flights", {
                        "reservation_id": "M20IZO",
                        "cabin": "business",
                        "flights": [
                            {"flight_number": "HAT268", "date": "2024-05-22"},
                            {"flight_number": "HAT010", "date": "2024-05-22"},
                        ],
                        "payment_id": "credit_card_9074831",
                    }),
                ],
                "result": "M20IZO upgraded to business class; $1194 charged to credit_card_9074831.",
            },
        ],
        "final_answer": (
            "NQNU5R has been cancelled and M20IZO upgraded to business class, with $1194 "
            "charged to your card ending in 7334. IFOYYZ could not be cancelled: it is a "
            "basic economy booking without travel insurance made more than 24 hours ago, "
            "which the cancellation policy does not allow."
        ),
    },
    {
        "id": "retail-status",
        "domain": "retail",
        "match": ["#W9300146", "current status"],
        "steps": [
            {
                "description": "Authenticate the customer: look up the user id for Aarav Anderson, zip 19031",
                "calls": [
                    ("find_user_id_by_name_zip",
                     {"first_name": "Aarav", "last_name": "Anderson", "zip": "19031"}),
                ],
                "result": "Customer authenticated as user aarav_anderson_8794.",
            },
            {
                "description": "Get order #W9300146 and read its status and payment",
                "calls": [
                    ("get_order_details", {"order_id": "#W9300146"}),
                ],
                "result": "Order #W9300146 is pending; the customer paid $153.23 with gift card gift_card_7245904.",
            },
        ],
        "final_answer": "Your order #W9300146 is currently pending. You paid $153.23 for it, with your gift card.",
    },
]


def find_workload(goal):
    for w in WORKLOADS:
        if all(m in (goal or "") for m in w["match"]):
            return w
    return None


def script_plan(workload):
    return [{"id": i, "description": s["description"]} for i, s in enumerate(workload["steps"], 1)]


def script_step(workload, step_id):
    if isinstance(step_id, int) and 1 <= step_id <= len(workload["steps"]):
        return workload["steps"][step_id - 1]
    return None
