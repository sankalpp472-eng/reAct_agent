"""
Tool implementations the actor can call via Gemini function calling.

- web_search: live web search via the Tavily API (https://tavily.com)
- http_request: generic HTTP call to a specific known URL
- calculator: safe arithmetic evaluation (no arbitrary code execution)

Plus, when the actor is given a tau-bench "domain" ("retail" or "airline"),
that domain's tools instead - served by the <domain>-tools faasd function and
called through the gateway (see domain_tools()).
"""
import ast
import math
import operator
import os
import requests

from . import telemetry

TAVILY_SECRET_PATH = "/var/openfaas/secrets/tavily-api-key"

# Gateway the actor uses to reach the tau-bench tool functions. From inside a
# faasd function the gateway is at gateway.openfaas:8080; override with the
# TOOLS_GATEWAY_URL env var (e.g. http://<faasd-host>:8080) when testing locally.
TOOLS_GATEWAY_URL = os.environ.get("TOOLS_GATEWAY_URL", "http://gateway.openfaas:8080")
TAU_DOMAINS = ("retail", "airline")

# --- Tool declarations, passed to Gemini's function-calling API ---
# Each entry needs a top-level "type": "function" - the Interactions API
# rejects declarations without it ("The 'type' parameter is required at
# 'tools[N]'").
TOOL_DECLARATIONS = [
    {
        "type": "function",
        "name": "web_search",
        "description": (
            "Search the live web for current information. Use this for anything "
            "that might have changed since your training data, or that you're not "
            "certain about (prices, current events, facts about specific places/things)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Max number of results to return (default 5, max 10).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "http_request",
        "description": (
            "Make an HTTP request to a specific, known URL (e.g. a public REST API "
            "endpoint). Use this when you know the exact endpoint to call - for "
            "open-ended questions, use web_search instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "description": "HTTP method",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                },
                "url": {"type": "string", "description": "Full URL, must start with http:// or https://"},
                "headers": {
                    "type": "object",
                    "description": "Optional request headers as key/value pairs.",
                },
                "body": {
                    "type": "string",
                    "description": "Optional request body, as a raw string (e.g. JSON-encoded).",
                },
            },
            "required": ["method", "url"],
        },
    },
    {
        "type": "function",
        "name": "calculator",
        "description": (
            "Evaluate a numeric arithmetic expression. Supports + - * / ** % // "
            "parentheses, and basic math functions (sqrt, sin, cos, tan, log, log10, "
            "exp, abs, round, pow). Use this for any calculation instead of doing "
            "math yourself, since you are not reliable at arithmetic."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The arithmetic expression to evaluate, e.g. '(3 + 4) * sqrt(16)'",
                },
            },
            "required": ["expression"],
        },
    },
]


def execute_tool(name, args):
    """Dispatch a tool call by name. Always returns a JSON-serializable dict."""
    if name == "web_search":
        return _web_search(args.get("query", ""), args.get("max_results", 5))
    if name == "http_request":
        return _http_request(
            args.get("method", "GET"),
            args.get("url", ""),
            args.get("headers"),
            args.get("body"),
        )
    if name == "calculator":
        return _calculator(args.get("expression", ""))
    return {"error": f"Unknown tool: {name}"}


# ------------------------- tau-bench domain tools -------------------------

_domain_declarations = {}


def _call_tool_function(domain, body, timeout=30):
    t2 = telemetry.now_ms()  # request leaves the actor
    resp = requests.post(
        f"{TOOLS_GATEWAY_URL.rstrip('/')}/function/{domain}-tools", json=body, timeout=timeout
    )
    t5 = telemetry.now_ms()  # response back at the actor
    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text[:1000]}
    callee_timing = data.pop("_timing", None) if isinstance(data, dict) else None
    if "tool" in body:
        telemetry.record_tool_call(body["tool"], t2, t5, callee_timing)
    if resp.status_code >= 400:
        return {"error": data.get("error", f"HTTP {resp.status_code}")}
    return data


def domain_tools(domain):
    """(declarations, execute_fn) for a tau-bench domain. Declarations come
    from the tool function's list_tools (already in Gemini format) and are
    cached for the life of this process. execute_fn returns the tool
    function's {"tool", "output", "error"} body, or {"error": "..."}."""
    if domain not in TAU_DOMAINS:
        raise ValueError(f"Unknown domain {domain!r}, expected one of {TAU_DOMAINS}")
    if domain not in _domain_declarations:
        listed = _call_tool_function(domain, {"action": "list_tools"})
        if "tools" not in listed:
            raise RuntimeError(f"Could not list {domain}-tools: {listed.get('error')}")
        _domain_declarations[domain] = listed["tools"]

    def execute(name, args):
        return _call_tool_function(domain, {"tool": name, "arguments": args or {}})

    return _domain_declarations[domain], execute


# ----------------------------- web_search -----------------------------

def _get_tavily_key():
    if os.path.exists(TAVILY_SECRET_PATH):
        with open(TAVILY_SECRET_PATH) as f:
            return f.read().strip()
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise RuntimeError(
            "No Tavily API key found. Create an OpenFaaS secret named "
            "'tavily-api-key' or set the TAVILY_API_KEY env var."
        )
    return key


def _web_search(query, max_results=5):
    if not query:
        return {"error": "query is required"}
    try:
        max_results = max(1, min(int(max_results or 5), 10))
    except (TypeError, ValueError):
        max_results = 5

    api_key = _get_tavily_key()
    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    results = [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "content": (r.get("content") or "")[:1000],
        }
        for r in data.get("results", [])
    ]
    return {"results": results}


# ---------------------------- http_request -----------------------------

# Very basic SSRF guard: block obvious private/loopback hosts by literal IP prefix.
# NOTE: this is NOT a hardened defense (e.g. DNS rebinding can bypass it). It's
# fine for local experimentation; tighten this before exposing the actor publicly.
_BLOCKED_HOST_PREFIXES = ("127.", "10.", "192.168.", "169.254.")


def _http_request(method, url, headers=None, body=None):
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}

    host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if host == "localhost" or any(host.startswith(p) for p in _BLOCKED_HOST_PREFIXES):
        return {"error": f"Refusing to call internal/private host: {host}"}

    method = (method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return {"error": f"Unsupported method: {method}"}

    try:
        resp = requests.request(method, url, headers=headers or {}, data=body, timeout=15)
    except requests.RequestException as e:
        return {"error": str(e)}

    return {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": resp.text[:4000],  # cap what we feed back to the model
    }


# ------------------------------ calculator ------------------------------

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_ALLOWED_FUNCS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "pow": pow,
}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("Only numeric constants are allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ValueError("Only whitelisted functions are allowed")
        args = [_safe_eval(a) for a in node.args]
        return _ALLOWED_FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_NAMES:
            return _ALLOWED_NAMES[node.id]
        raise ValueError(f"Unknown name: {node.id}")
    raise ValueError(f"Disallowed expression: {ast.dump(node)}")


def _calculator(expression):
    if not expression:
        return {"error": "expression is required"}
    try:
        tree = ast.parse(expression, mode="eval")
        return {"result": _safe_eval(tree)}
    except Exception as e:
        return {"error": f"Could not evaluate expression: {e}"}
