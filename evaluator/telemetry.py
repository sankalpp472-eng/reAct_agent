"""
Per-request timing for the Experiment 1 metrics. Keep this file identical in
planner/, actor/, evaluator/, retail-tools/ and airline-tools/.

Each handler calls begin() when it is entered (t3) and attach() on the way out
(t4), which adds a `_timing` object to the response body:

  {
    "t3": <epoch ms, handler entered>,
    "t4": <epoch ms, handler about to return>,
    "handler_ms": t4 - t3,               # Twarm for this function
    "llm_ms": <total simulated/real LLM time in this request>,
    "llm_calls": <number of LLM calls>,
    "llm_wait_ms": <part of llm_ms spent waiting out API rate limits (real LLM only)>,
    "tool_calls": [                      # actor only: calls to the tool functions
      {"tool", "t2", "t3", "t4", "t5", "http_ms", "twarm_ms", "troute_ms"}, ...
    ]
  }

The caller (Conductor, the actor, or a driver script) combines this with its own
send/receive timestamps (t2/t5). Every metric is a difference of timestamps taken
on the same clock, so clock skew between hosts doesn't matter.

waitress serves each request on its own thread, so state is thread-local.
"""
import threading
import time

_local = threading.local()


def now_ms():
    return time.time() * 1000.0


def begin():
    _local.timing = {"t3": now_ms(), "llm_ms": 0.0, "llm_calls": 0, "llm_wait_ms": 0.0,
                     "tool_calls": []}


def record_llm(ms, wait_ms=0.0):
    """ms: one LLM call's wall time. wait_ms: the part of it spent sleeping on
    API rate limits (429 + Retry-After), so the metrics can leave it out."""
    timing = getattr(_local, "timing", None)
    if timing is not None:
        timing["llm_ms"] += ms
        timing["llm_calls"] += 1
        timing["llm_wait_ms"] += wait_ms


def record_tool_call(tool, t2, t5, callee_timing, error=None, attempts=1):
    """t2/t5: when the caller sent the request / got the response.
    callee_timing: the tool function's own `_timing` (for its t3/t4).
    error: why the call failed (exception, or HTTP status + body), if it did.
    attempts: requests sent (> 1 when a cold function was retried)."""
    timing = getattr(_local, "timing", None)
    if timing is None:
        return
    entry = {"tool": tool, "t2": t2, "t5": t5, "http_ms": t5 - t2}
    if error:
        entry["error"] = error
    if attempts > 1:
        entry["attempts"] = attempts
    if callee_timing:
        twarm = callee_timing["t4"] - callee_timing["t3"]
        entry.update(
            t3=callee_timing["t3"], t4=callee_timing["t4"],
            twarm_ms=twarm, troute_ms=(t5 - t2) - twarm,
        )
    timing["tool_calls"].append(entry)


def attach(body):
    """Stamp t4 and add `_timing` to a dict response body."""
    timing = getattr(_local, "timing", None)
    if timing is None or not isinstance(body, dict):
        return body
    _local.timing = None
    timing["t4"] = now_ms()
    timing["handler_ms"] = timing["t4"] - timing["t3"]
    if not timing["tool_calls"]:
        del timing["tool_calls"]
    return {**body, "_timing": timing}
