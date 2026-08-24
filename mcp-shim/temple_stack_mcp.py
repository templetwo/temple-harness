#!/usr/bin/env python3
"""Temple Stack MCP shim — read-only chronicle access over the local REST bridge.

A thin MCP (Model Context Protocol) stdio server that wraps the Sovereign Stack's
local bridge (POST http://127.0.0.1:8100/api/call) so MCP-capable harnesses — the
DeepSeek Harness (dsh) in particular — can give local models chronicle RECALL
without ever handing them the write lane.

Design constraints, all deliberate:

  * STDLIB ONLY. Runs anywhere python3 exists. No pip, no venv, no wheels.
  * READ-ONLY BY CONSTRUCTION. Exactly four MCP tools, each pinned to one
    allowlisted bridge target (see BRIDGE_TARGETS / ALLOWED_BRIDGE_TOOLS).
    There is no pass-through tool. A caller cannot name a bridge tool; it can
    only pick one of four doors that were opened for it.
  * FAIL CLOSED, SPEAK PLAINLY. An unreachable bridge produces a tool result
    that says so. A missing token stops the process at startup. Nothing here
    returns an empty success.
  * STATE COVERAGE. Both the bridge's own partiality (returned N of M matched)
    and this shim's character cap are reported in the rendered text. Silent
    truncation is the house anti-pattern this shim exists downstream of.

Transport: newline-delimited JSON-RPC 2.0 over stdio, per the MCP stdio spec.
One JSON object per line on stdout; diagnostics go to stderr ONLY — anything
printed to stdout corrupts the protocol stream.

Environment:
  TEMPLE_BRIDGE_URL       base URL of the bridge   (default http://127.0.0.1:8100)
  TEMPLE_BRIDGE_TOKEN     token override; skips the env file entirely (tests use this)
  TEMPLE_BRIDGE_ENV_FILE  env file to parse        (default ~/.config/sovereign-bridge.env)
  TEMPLE_MCP_MAX_CHARS    per-result character cap (default 4000)
  TEMPLE_MCP_TIMEOUT      bridge HTTP timeout, sec (default 20)
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Identity and protocol
# --------------------------------------------------------------------------

SERVER_NAME = "temple-stack"
SERVER_VERSION = "0.2.0"

# MCP stdio transport is newline-delimited JSON-RPC 2.0 (NOT Content-Length
# framed — that is LSP). Versions this shim knows how to speak, newest first.
PREFERRED_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 error codes
E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8100"
DEFAULT_ENV_FILE = "~/.config/sovereign-bridge.env"
DEFAULT_MAX_CHARS = 4000
DEFAULT_TIMEOUT = 20.0
TOKEN_ENV_KEY = "BRIDGE_TOKEN"

LIMIT_DEFAULT = 5
LIMIT_MAX = 10

# --------------------------------------------------------------------------
# THE READ-ONLY BOUNDARY
#
# BRIDGE_TARGETS is the whole surface. Three MCP tools, three bridge targets,
# nothing else reachable. ALLOWED_BRIDGE_TOOLS is the enforcement point for the
# POST /api/call lane — bridge_call() refuses any name outside it BEFORE it
# builds a request, so a bug elsewhere cannot widen the scope by accident.
# ALLOWED_BRIDGE_PATHS does the same for the unauthenticated GET lane.
#
# Adding a write tool (record_insight, handoff, close_session, ...) requires
# editing these constants. That is the point: the boundary is a diff, not a
# runtime decision, and the diff is reviewable.
# --------------------------------------------------------------------------

BRIDGE_TARGETS = {
    "stack_recall": ("POST", "recall_insights"),
    # stack_latest reuses the SAME allowlisted read target as stack_recall — no
    # new bridge surface. It is the sanctioned recency door: a query-less tail
    # read (order=newest, no search terms), which is a different question from
    # the newest-ordered *search* that stack_recall deliberately pins away.
    "stack_latest": ("POST", "recall_insights"),
    "stack_open_threads": ("POST", "get_open_threads"),
    "stack_heartbeat": ("GET", "/api/heartbeat"),
}

ALLOWED_BRIDGE_TOOLS = frozenset({"recall_insights", "get_open_threads"})
ALLOWED_BRIDGE_PATHS = frozenset({"/api/heartbeat"})


class BridgeToolNotAllowed(Exception):
    """A bridge tool outside the read-only allowlist was requested."""


class BridgeError(Exception):
    """The bridge could not be reached, or answered with something unusable."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def bridge_url() -> str:
    return (os.environ.get("TEMPLE_BRIDGE_URL") or DEFAULT_BRIDGE_URL).rstrip("/")


def bridge_timeout() -> float:
    raw = os.environ.get("TEMPLE_MCP_TIMEOUT")
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def max_chars() -> int:
    raw = os.environ.get("TEMPLE_MCP_MAX_CHARS")
    try:
        value = int(raw) if raw else DEFAULT_MAX_CHARS
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return value if value > 0 else DEFAULT_MAX_CHARS


def parse_env_file(path: str) -> dict:
    """Parse a shell-sourceable KEY=VALUE env file. Values are never logged."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key:
                    values[key] = value
    except OSError:
        return {}
    return values


def load_token() -> str | None:
    """Resolve the bridge token. Called lazily — never at import time.

    Order: TEMPLE_BRIDGE_TOKEN override (tests) -> env file BRIDGE_TOKEN.
    The value is returned, never printed, never logged, never put in a URL.
    """
    override = os.environ.get("TEMPLE_BRIDGE_TOKEN")
    if override:
        return override
    path = os.path.expanduser(os.environ.get("TEMPLE_BRIDGE_ENV_FILE") or DEFAULT_ENV_FILE)
    token = parse_env_file(path).get(TOKEN_ENV_KEY)
    return token or None


# --------------------------------------------------------------------------
# Bridge transport
# --------------------------------------------------------------------------


def _http_json(method: str, url: str, *, body=None, token=None, timeout=None):
    """One HTTP round trip returning parsed JSON, or raise BridgeError."""
    data = None
    headers = {"Accept": "application/json", "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout or bridge_timeout()) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # subclass of URLError — must come first
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:400]
        except Exception:  # noqa: BLE001 - detail is best-effort
            detail = ""
        raise BridgeError(
            f"bridge answered HTTP {exc.code} ({exc.reason}) for {method} {url}"
            + (f" — {detail}" if detail else "")
        ) from None
    except urllib.error.URLError as exc:
        raise BridgeError(f"bridge unreachable at {url} — {exc.reason}") from None
    except TimeoutError:
        raise BridgeError(f"bridge timed out at {url} after {timeout or bridge_timeout()}s") from None
    except socket.timeout:  # pragma: no cover - alias of TimeoutError on 3.10+
        raise BridgeError(f"bridge timed out at {url}") from None
    except OSError as exc:
        raise BridgeError(f"bridge transport error at {url} — {exc}") from None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise BridgeError(f"bridge returned non-JSON from {method} {url}: {raw[:200]!r}") from None


def bridge_call(bridge_tool: str, arguments: dict | None = None, *, token=None, base_url=None, timeout=None) -> dict:
    """Forward ONE allowlisted read tool to POST /api/call.

    Refuses, by construction and before any network work, any bridge tool that
    is not in ALLOWED_BRIDGE_TOOLS. This is the read-only boundary's teeth.
    """
    if bridge_tool not in ALLOWED_BRIDGE_TOOLS:
        raise BridgeToolNotAllowed(
            f"refused: '{bridge_tool}' is not in this shim's read-only allowlist "
            f"({', '.join(sorted(ALLOWED_BRIDGE_TOOLS))}). This shim has no write lane."
        )

    token = token if token is not None else load_token()
    if not token:
        raise BridgeError(
            "no bridge token available (set TEMPLE_BRIDGE_TOKEN or provide "
            f"{TOKEN_ENV_KEY} in {os.environ.get('TEMPLE_BRIDGE_ENV_FILE') or DEFAULT_ENV_FILE})"
        )

    base = (base_url or bridge_url()).rstrip("/")
    payload = {"tool": bridge_tool, "arguments": dict(arguments or {})}
    data = _http_json("POST", base + "/api/call", body=payload, token=token, timeout=timeout)

    if not isinstance(data, dict):
        raise BridgeError(f"bridge returned a non-object envelope for '{bridge_tool}'")
    if data.get("ok") is not True:
        raise BridgeError(f"bridge reported not-ok for '{bridge_tool}': {json.dumps(data)[:300]}")

    result = data.get("result")
    # The bridge answers HTTP 200 with ok:true and a STRING result when the tool
    # name is unrecognised ("Unknown tool: ..."). Both allowlisted tools return
    # an object; a string here is therefore an error wearing a success costume.
    if isinstance(result, str):
        raise BridgeError(f"bridge returned an error message for '{bridge_tool}' under HTTP 200: {result}")
    if not isinstance(result, dict):
        raise BridgeError(f"bridge returned an unexpected result type ({type(result).__name__}) for '{bridge_tool}'")
    return result


def bridge_heartbeat(*, base_url=None, timeout=None) -> dict:
    """GET the unauthenticated heartbeat. No token, no side effects."""
    path = "/api/heartbeat"
    if path not in ALLOWED_BRIDGE_PATHS:  # pragma: no cover - constant guard
        raise BridgeToolNotAllowed(f"refused: '{path}' is not an allowlisted bridge path")
    base = (base_url or bridge_url()).rstrip("/")
    data = _http_json("GET", base + path, timeout=timeout)
    if not isinstance(data, dict):
        raise BridgeError("bridge heartbeat returned a non-object body")
    return data


# --------------------------------------------------------------------------
# Rendering — plain text, budgeted, coverage always stated
# --------------------------------------------------------------------------


def truncate(text: str, cap: int | None = None) -> str:
    """Cap text and SAY SO. The marker is appended, never itself cut."""
    cap = cap if cap is not None else max_chars()
    total = len(text)
    if cap <= 0 or total <= cap:
        return text
    return text[:cap] + f"\n\n[truncated, {cap} of {total} chars]"


def _clamp_limit(value, default: int = LIMIT_DEFAULT, maximum: int = LIMIT_MAX) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, number))


def _short_ts(value) -> str:
    text = str(value or "").strip()
    return text[:19] if len(text) > 19 else text or "(no timestamp)"


def _coverage_line(result: dict) -> str:
    """Restate the BRIDGE's own partiality. Distinct from this shim's char cap."""
    returned = result.get("returned")
    if returned is None:
        returned = len(result.get("items") or [])
    total = result.get("total_matched")
    parts = [f"bridge coverage: returned {returned} of {total} matched" if total is not None
             else f"bridge coverage: returned {returned}"]
    if result.get("truncated"):
        reasons = result.get("partial_reasons") or []
        parts.append("bridge-side truncated" + (f" ({', '.join(str(r) for r in reasons)})" if reasons else ""))
    continuation = result.get("continuation")
    if isinstance(continuation, dict) and continuation.get("offset") is not None:
        parts.append(f"more available from offset {continuation['offset']}")
    scope = result.get("scope")
    if isinstance(scope, dict) and scope.get("domains_searched") is not None:
        parts.append(f"domains searched {scope['domains_searched']}/{scope.get('domains_total', '?')}")
    return " | ".join(parts)


def _insight_blocks(items: list) -> list:
    """Render chronicle insight entries as numbered blocks (shared by recall/latest)."""
    blocks = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            blocks.append(f"[{index}] (unreadable entry)")
            continue
        meta = [
            _short_ts(item.get("timestamp")),
            f"domain: {item.get('domain') or '(none)'}",
            f"layer: {item.get('layer') or '(none)'}",
        ]
        if item.get("intensity") is not None:
            meta.append(f"intensity: {item['intensity']}")
        receipts = item.get("verified_by") or []
        if receipts:
            meta.append(f"receipts: {len(receipts)}")
        if item.get("claim_id"):
            meta.append(f"claim: {item['claim_id']}")
        blocks.append(f"[{index}] " + " · ".join(meta) + "\n" + str(item.get("content") or "").strip())
    return blocks


def render_recall(result: dict, query: str, domain: str | None, limit: int) -> str:
    items = result.get("items") or []
    header = [
        f'Sovereign Stack recall — query: "{query}"'
        + (f' | domain: "{domain}"' if domain else " | domain: (all)")
        + f" | limit: {limit} | order: relevance",
        _coverage_line(result),
    ]
    if not items:
        header.append("")
        header.append("No matching chronicle entries.")
        return "\n".join(header)
    return "\n".join(header) + "\n\n" + "\n\n".join(_insight_blocks(items))


def render_latest(result: dict, domain: str | None, limit: int) -> str:
    items = result.get("items") or []
    header = [
        f"Sovereign Stack latest — the {limit} newest chronicle entries"
        + (f' | domain: "{domain}"' if domain else " | domain: (all)")
        + " | order: newest",
        _coverage_line(result),
    ]
    if not items:
        header.append("")
        header.append("No chronicle entries.")
        return "\n".join(header)
    return "\n".join(header) + "\n\n" + "\n\n".join(_insight_blocks(items))


def render_open_threads(result: dict, limit: int) -> str:
    items = result.get("items") or []
    header = [f"Sovereign Stack open threads — limit: {limit}", _coverage_line(result)]
    if not items:
        header.append("")
        header.append("No open threads.")
        return "\n".join(header)

    blocks = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            blocks.append(f"[{index}] (unreadable entry)")
            continue
        meta = [
            _short_ts(item.get("timestamp")),
            f"domain: {item.get('domain') or '(none)'}",
            f"id: {item.get('thread_id') or '(none)'}",
        ]
        if item.get("touch_count"):
            meta.append(f"touches: {item['touch_count']}")
        body = str(item.get("question") or "").strip()
        context = str(item.get("context") or "").strip()
        if context:
            body += "\n  context: " + context
        blocks.append(f"[{index}] " + " · ".join(meta) + "\n" + body)
    return "\n".join(header) + "\n\n" + "\n\n".join(blocks)


def render_heartbeat(data: dict) -> str:
    lines = [
        "Sovereign Stack bridge heartbeat",
        f"status: {data.get('status', '(unknown)')}",
        f"version: {data.get('version', '(unknown)')}",
        f"tools: {data.get('tools', '(unknown)')}",
    ]
    for key, label in (("source_commit", "source_commit"), ("server_time_utc", "server_time_utc")):
        if data.get(key) is not None:
            lines.append(f"{label}: {data[key]}")
    lines.append(f"bridge_url: {bridge_url()}")
    lines.append("scope: READ-ONLY (recall, latest, open threads, heartbeat)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# MCP tool definitions and handlers
# --------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "stack_recall",
        "title": "Recall Sovereign Stack insights",
        "description": (
            "Search the Sovereign Stack chronicle for insights matching a query. "
            "Read-only. Results are always ordered by RELEVANCE (the bridge default, "
            "'newest', returns recency noise for historical questions — this shim pins "
            "relevance and does not accept an order argument). Matching is keyword-OR "
            "across the query terms. Coverage (how many of the total matches you are "
            "seeing) is stated in every result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms. Keyword-OR across the chronicle."},
                "domain": {"type": "string", "description": "Optional domain filter. Matching is subset-based, so a compound domain is reachable by any one component."},
                "limit": {"type": "integer", "minimum": 1, "maximum": LIMIT_MAX, "default": LIMIT_DEFAULT, "description": f"Entries to return, 1-{LIMIT_MAX}. Default {LIMIT_DEFAULT}."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "stack_latest",
        "title": "Newest chronicle entries",
        "description": (
            "List the NEWEST entries in the Sovereign Stack chronicle, most recent "
            "first. Use this ONLY for what-happened-recently questions ('what's the "
            "latest?', 'what happened today?'). It takes NO query — for any topical "
            "or historical question, use stack_recall instead. Read-only. Coverage "
            "is stated in every result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Optional domain filter. Matching is subset-based, so a compound domain is reachable by any one component."},
                "limit": {"type": "integer", "minimum": 1, "maximum": LIMIT_MAX, "default": LIMIT_DEFAULT, "description": f"Entries to return, 1-{LIMIT_MAX}. Default {LIMIT_DEFAULT}."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "stack_open_threads",
        "title": "List open threads",
        "description": (
            "List currently open threads (unresolved questions) from the Sovereign Stack "
            "chronicle. Read-only. Coverage is stated in every result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": LIMIT_MAX, "default": LIMIT_DEFAULT, "description": f"Threads to return, 1-{LIMIT_MAX}. Default {LIMIT_DEFAULT}."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "stack_heartbeat",
        "title": "Check bridge health",
        "description": (
            "Check that the Sovereign Stack bridge is alive. Returns status, version and "
            "tool count. Unauthenticated, read-only, no side effects."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
]


def call_tool(name: str, arguments: dict) -> str:
    """Dispatch one MCP tool call to its allowlisted bridge target. Returns text."""
    arguments = arguments if isinstance(arguments, dict) else {}

    if name == "stack_recall":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("stack_recall requires a non-empty 'query' string")
        domain = arguments.get("domain")
        domain = domain.strip() if isinstance(domain, str) and domain.strip() else None
        limit = _clamp_limit(arguments.get("limit"))
        # order is NOT taken from arguments and is NOT in the input schema:
        # relevance is pinned so a caller cannot fall back into recency noise.
        forwarded = {"query": query.strip(), "limit": limit, "order": "relevance"}
        if domain:
            forwarded["domain"] = domain
        result = bridge_call("recall_insights", forwarded)
        return render_recall(result, query.strip(), domain, limit)

    if name == "stack_latest":
        # The groove-guard, enforced server-side and not just in the schema: a
        # query here means the caller wanted stack_recall — say so, plainly.
        if "query" in arguments:
            raise ValueError(
                "stack_latest takes no 'query' — it returns the newest entries only. "
                "For a topical search, use stack_recall."
            )
        domain = arguments.get("domain")
        domain = domain.strip() if isinstance(domain, str) and domain.strip() else None
        limit = _clamp_limit(arguments.get("limit"))
        # order is pinned to newest and is NOT in the input schema; this door has
        # no search terms, so newest-first here is a tail read, not the
        # recency-noise trap stack_recall pins away.
        forwarded = {"limit": limit, "order": "newest"}
        if domain:
            forwarded["domain"] = domain
        result = bridge_call("recall_insights", forwarded)
        return render_latest(result, domain, limit)

    if name == "stack_open_threads":
        limit = _clamp_limit(arguments.get("limit"))
        result = bridge_call("get_open_threads", {"limit": limit})
        return render_open_threads(result, limit)

    if name == "stack_heartbeat":
        return render_heartbeat(bridge_heartbeat())

    raise BridgeToolNotAllowed(
        f"refused: '{name}' is not one of this shim's read-only tools "
        f"({', '.join(sorted(BRIDGE_TARGETS))})."
    )


# --------------------------------------------------------------------------
# JSON-RPC 2.0 plumbing
# --------------------------------------------------------------------------


def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def negotiate_protocol_version(requested) -> str:
    """Accept what the client offers when we speak it; otherwise state ours."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PREFERRED_PROTOCOL_VERSION


def handle_message(message: dict) -> dict | None:
    """Handle one JSON-RPC message. Returns a response, or None for notifications."""
    if not isinstance(message, dict):
        return _error(None, E_INVALID_REQUEST, "request must be a JSON object")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    is_notification = "id" not in message

    if not isinstance(method, str):
        return None if is_notification else _error(request_id, E_INVALID_REQUEST, "missing 'method'")

    if method == "initialize":
        payload = {
            "protocolVersion": negotiate_protocol_version(params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Read-only access to the Temple of Two's Sovereign Stack chronicle. "
                "stack_recall searches insights (relevance-ordered), stack_latest lists "
                "the newest entries (no query — recency questions only), "
                "stack_open_threads lists unresolved questions, stack_heartbeat checks "
                "the bridge. There is no write tool and no pass-through: this shim "
                "cannot record anything. Every result states its coverage — read the "
                "coverage line, not just the hits."
            ),
        }
        return _result(request_id, payload)

    if method.startswith("notifications/"):
        return None

    if is_notification:
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str):
            return _error(request_id, E_INVALID_PARAMS, "tools/call requires a string 'name'")
        try:
            text = call_tool(name, arguments if isinstance(arguments, dict) else {})
        except BridgeToolNotAllowed as exc:
            return _result(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except BridgeError as exc:
            return _result(
                request_id,
                {
                    "content": [{
                        "type": "text",
                        "text": f"Sovereign Stack unavailable — {exc}\n\nNo chronicle data was returned. "
                                "This is a failed call, not an empty result.",
                    }],
                    "isError": True,
                },
            )
        except ValueError as exc:
            return _result(request_id, {"content": [{"type": "text", "text": f"Invalid arguments — {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - never crash the transport
            return _result(
                request_id,
                {"content": [{"type": "text", "text": f"Shim error handling '{name}' — {type(exc).__name__}: {exc}"}], "isError": True},
            )
        return _result(request_id, {"content": [{"type": "text", "text": truncate(text)}], "isError": False})

    return _error(request_id, E_METHOD_NOT_FOUND, f"unknown method: {method}")


def _write(stream, message: dict) -> None:
    stream.write(json.dumps(message, separators=(",", ":")) + "\n")
    stream.flush()


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    while True:
        try:
            line = stdin.readline()
        except (KeyboardInterrupt, ValueError):
            return 0
        if not line:  # EOF — the client closed the pipe
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(stdout, _error(None, E_PARSE, f"invalid JSON: {exc}"))
            continue
        if isinstance(message, list):
            # JSON-RPC batching was removed in MCP 2025-06-18.
            _write(stdout, _error(None, E_INVALID_REQUEST, "batch requests are not supported"))
            continue
        try:
            response = handle_message(message)
        except Exception as exc:  # noqa: BLE001 - the transport must survive anything
            response = _error(message.get("id") if isinstance(message, dict) else None, E_INTERNAL, f"{type(exc).__name__}: {exc}")
        if response is not None:
            _write(stdout, response)


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    if argv and argv[0] == "--version":
        sys.stderr.write(f"{SERVER_NAME} {SERVER_VERSION}\n")
        return 0

    # Fail closed at startup: no token, no server. One line, stderr, no value.
    if not load_token():
        env_file = os.environ.get("TEMPLE_BRIDGE_ENV_FILE") or DEFAULT_ENV_FILE
        sys.stderr.write(
            f"{SERVER_NAME}: no bridge token — set TEMPLE_BRIDGE_TOKEN or provide "
            f"{TOKEN_ENV_KEY}=<value> in {env_file}\n"
        )
        return 1

    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    return serve()


if __name__ == "__main__":
    sys.exit(main())
