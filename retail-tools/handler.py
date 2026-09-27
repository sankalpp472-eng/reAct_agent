"""
tau-bench tool server. Exposes every tool of one tau-bench domain (retail or
airline) as a single faasd function, backed by that domain's mock database.

The tool implementations under tau_tools/ and the JSON database under data/
are vendored unchanged from https://github.com/sierra-research/tau-bench
(MIT, see LICENSE-tau-bench) - only the `Tool` base-class import was
rewritten so the package is self-contained. This file is identical in
retail-tools/ and airline-tools/; the domain is determined by what's in
tau_tools/ and data/.

Why one function per domain rather than one per tool: several tools mutate
the database (cancel an order, book a flight, ...) and later tools must see
those writes. Keeping all of a domain's tools in one process keeps that
state consistent - faasd runs exactly one replica per function, so there is
a single in-memory copy of the DB.

Input (JSON body), one of:
  {"tool": "<name>", "arguments": {...}}   invoke a tool
  {"action": "list_tools"}                  tool declarations (Gemini Interactions
                                            API format, ready for the actor)
  {"action": "reset"}                       reload the DB from disk (start of a task)
  {"action": "hash"}                        tau-bench DB hash, for reward checking
A GET with no body is treated as list_tools.

Output (JSON body) for a tool call:
  {"tool": "<name>", "output": "<string the tool returned>", "error": bool}
Like in tau-bench, tool outputs are always strings; failures come back as
"Error: ..." strings (HTTP 200) so the model can read and react to them.
Only malformed requests (unknown tool, missing fields) return HTTP 4xx.
"""
import glob
import json
import os
import threading
from hashlib import sha256

from . import telemetry
from .tau_tools import ALL_TOOLS

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

TOOLS_MAP = {t.get_info()["function"]["name"]: t for t in ALL_TOOLS}

# waitress serves requests on multiple threads; tools read-modify-write the
# shared DB dict, so serialize access to it.
_lock = threading.Lock()
_data = None


def _load_data():
    """{"orders": ..., "products": ..., "users": ...} for retail,
    {"flights": ..., "reservations": ..., "users": ...} for airline - the same
    shape tau-bench's data/__init__.py load_data() builds."""
    data = {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.json"))):
        with open(path) as f:
            data[os.path.splitext(os.path.basename(path))[0]] = json.load(f)
    return data


def _get_data():
    global _data
    if _data is None:
        _data = _load_data()
    return _data


def _reset():
    global _data
    with _lock:
        _data = _load_data()


def _declarations():
    """tau-bench declares tools in the OpenAI shape
    {"type": "function", "function": {name, description, parameters}};
    the Gemini Interactions API used by the actor wants it flattened to
    {"type": "function", name, description, parameters}."""
    return [{"type": "function", **t.get_info()["function"]} for t in ALL_TOOLS]


# Same hashing tau-bench's Env uses to compare the final DB against the
# expected one when scoring a task.
def _to_hashable(item):
    if isinstance(item, dict):
        return tuple((k, _to_hashable(v)) for k, v in sorted(item.items()))
    if isinstance(item, list):
        return tuple(_to_hashable(e) for e in item)
    if isinstance(item, set):
        return tuple(sorted(_to_hashable(e) for e in item))
    return item


def _data_hash():
    return sha256(str(_to_hashable(_get_data())).encode("utf-8")).hexdigest()


def handle(event, context):
    telemetry.begin()  # t3
    try:
        payload = _parse_body(event.body)
        if not payload and event.method == "GET":
            payload = {"action": "list_tools"}

        tool_name = payload.get("tool")
        action = payload.get("action")

        if tool_name:
            return _invoke(tool_name, payload.get("arguments") or {})
        if action == "list_tools":
            return _resp(200, {"tools": _declarations()})
        if action == "reset":
            _reset()
            return _resp(200, {"status": "reset"})
        if action == "hash":
            with _lock:
                return _resp(200, {"hash": _data_hash()})

        return _resp(
            400,
            {"error": "Send {'tool': name, 'arguments': {...}} or "
                      "{'action': 'list_tools' | 'reset' | 'hash'}"},
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})


def _invoke(tool_name, arguments):
    tool = TOOLS_MAP.get(tool_name)
    if tool is None:
        return _resp(
            404,
            {"error": f"Unknown tool: {tool_name}", "available": sorted(TOOLS_MAP)},
        )
    if not isinstance(arguments, dict):
        return _resp(400, {"error": "'arguments' must be a JSON object"})

    with _lock:
        try:
            output = tool.invoke(data=_get_data(), **arguments)
        except Exception as e:
            # Mirrors tau-bench's Env.step: bad arguments become an error
            # observation for the model rather than a crashed request.
            output = f"Error: {e}"

    return _resp(
        200,
        {
            "tool": tool_name,
            "output": output,
            "error": isinstance(output, str) and output.startswith("Error"),
        },
    )


def _parse_body(body):
    if isinstance(body, (dict, list)):
        return body
    if not body:
        return {}
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def _resp(status_code, body_dict):
    return {"statusCode": status_code, "body": telemetry.attach(body_dict)}
