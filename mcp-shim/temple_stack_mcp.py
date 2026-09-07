#!/usr/bin/env python3
"""Temple Stack MCP shim — read-only chronicle access over the local REST bridge.

A thin MCP (Model Context Protocol) stdio server that wraps the Sovereign Stack's
local bridge so MCP-capable harnesses — the DeepSeek Harness (dsh) in particular
— can give local models chronicle RECALL, ARRIVAL, STANDING LAW and the WATCH
QUEUE without ever handing them the write lane.

Design constraints, all deliberate:

  * STDLIB ONLY. Runs anywhere python3 exists. No pip, no venv, no wheels.
  * READ-ONLY BY CONSTRUCTION. Exactly seven MCP tools, each pinned to one
    allowlisted bridge target (see BRIDGE_TARGETS / ALLOWED_BRIDGE_TOOLS).
    There is no pass-through tool. A caller cannot name a bridge tool; it can
    only pick one of seven doors that were opened for it.
  * THIS SHIM NEVER CARRIES THE MASTER KEY. Two transports, chosen by
    configuration and never by guessing: the Studio SEAT SOCKET (no credential
    at all) or a SCOPED GRANT token. The old env-file fallback, which loaded
    the master bridge token out of ~/.config/sovereign-bridge.env, is REMOVED
    and its variable is now refused by name.
  * FAIL CLOSED, SPEAK PLAINLY. An unreachable bridge produces a tool result
    that says so. An unresolvable transport stops the process at startup.
    Nothing here returns an empty success.
  * STATE COVERAGE. Both the source's own partiality and this shim's character
    cap are reported in the rendered text, separately and never conflated.
    Silent truncation is the house anti-pattern this shim exists downstream of.

Transport: newline-delimited JSON-RPC 2.0 over stdio, per the MCP stdio spec.
One JSON object per line on stdout; diagnostics go to stderr ONLY — anything
printed to stdout corrupts the protocol stream.

Environment:
  SOVEREIGN_SEAT          the seat id of a seated Studio terminal. Its PRESENCE
                          selects the seat-socket transport and its VALUE is
                          sent as X-Sovereign-Seat. Set by Anthony's launchers.
  TEMPLE_BRIDGE_SOCKET    seat-socket path override (default
                          ~/.sovereign/hq/seats/sock/bridge.sock)
  TEMPLE_BRIDGE_TOKEN     a SCOPED GRANT token. The only token form this shim
                          accepts, and it is mutually exclusive with the seat
                          transport.
  TEMPLE_BRIDGE_URL       base URL for the grant transport (default
                          http://127.0.0.1:8100)
  TEMPLE_SEAT_NAME        the BARE MODEL NAME stack_arrive announces itself
                          with, so that model line's to_self letters route to
                          it. NOT a seat id — see stack_arrive.
  TEMPLE_MCP_MAX_CHARS    per-result character cap (default 4000)
  TEMPLE_MCP_TIMEOUT      bridge HTTP timeout, sec (default 20)
  TEMPLE_BRIDGE_ENV_FILE  REFUSED. Named here so the refusal is legible.
"""

from __future__ import annotations

import http.client
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
SERVER_VERSION = "0.4.0"

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
DEFAULT_SEAT_SOCKET = "~/.sovereign/hq/seats/sock/bridge.sock"
DEFAULT_MAX_CHARS = 4000
DEFAULT_TIMEOUT = 20.0

# The environment variables the transport is chosen from. Named as constants so
# every message that mentions one spells it the same way.
SEAT_ENV_VAR = "SOVEREIGN_SEAT"
SOCKET_ENV_VAR = "TEMPLE_BRIDGE_SOCKET"
TOKEN_ENV_VAR = "TEMPLE_BRIDGE_TOKEN"
SEAT_NAME_ENV_VAR = "TEMPLE_SEAT_NAME"

# ⚠ REFUSED, NOT IGNORED. Until 0.4.0 this variable named a shell-sourceable
# file the shim parsed for BRIDGE_TOKEN — the MASTER key. Anthony's rule of
# 2026-09-05 forbids the master key as a seat credential everywhere: inside the
# Studio a seated terminal uses the socket, and outside it a seat asks for a
# scoped grant. A silently ignored variable would leave an operator believing
# they had configured a credential, so its presence is a startup refusal that
# names the two real doors.
ENV_FILE_VAR = "TEMPLE_BRIDGE_ENV_FILE"

LIMIT_DEFAULT = 5
LIMIT_MAX = 10

# stack_arrive's per-bucket cap. The stack accepts 1..100 and REFUSES anything
# outside that (it does not clamp), so this shim's own ceiling stays well inside
# the stack's and clamps rather than refuses — consistent with `limit`.
BUCKET_DEFAULT = 5
BUCKET_MAX = 20

# --------------------------------------------------------------------------
# THE READ-ONLY BOUNDARY
#
# BRIDGE_TARGETS is the whole surface. Seven MCP tools, five bridge targets,
# nothing else reachable. ALLOWED_BRIDGE_TOOLS is the enforcement point for the
# POST /api/call lane — the post helper refuses any name outside it BEFORE it
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
    "stack_arrive": ("POST", "arrive_lineage"),
    "stack_policies": ("POST", "current_policies"),
    "stack_signals": ("POST", "signals_summary"),
    "stack_heartbeat": ("GET", "/api/heartbeat"),
}

ALLOWED_BRIDGE_TOOLS = frozenset(
    {
        "recall_insights",
        "get_open_threads",
        "arrive_lineage",
        "current_policies",
        "signals_summary",
    }
)
ALLOWED_BRIDGE_PATHS = frozenset({"/api/heartbeat"})

# ⚠ THE RESULT-TYPE PIN, AND IT IS PART OF THE BOUNDARY.
#
# The bridge parses a tool's TextContent with json.loads and falls back to the
# raw string, so `result` is an OBJECT for tools that emit JSON and a STRING for
# tools that emit rendered prose. Both are legitimate; conflating them is not.
#
#   * A STRING where an object is expected is the bridge's documented fail-open
#     costume (HTTP 200 + ok:true + "Unknown tool: X" on a stack old enough to
#     return that instead of raising) and is refused.
#   * An OBJECT where a string is expected means the tool's output shape changed
#     under us, and is refused too. Without this half, a text renderer would
#     stringify a dict and print plausible garbage.
#
# Every allowlisted POST name must be classified, and the classification is
# checked by test rather than trusted: an unclassified addition is a red suite,
# not a silently unguarded door.
TEXT_RESULT_TOOLS = frozenset({"arrive_lineage", "current_policies"})
JSON_RESULT_TOOLS = frozenset({"recall_insights", "get_open_threads", "signals_summary"})


class BridgeToolNotAllowed(Exception):
    """A bridge tool outside the read-only allowlist was requested."""


class BridgeError(Exception):
    """The bridge could not be reached, or answered with something unusable."""


class TransportRefused(Exception):
    """No transport could be resolved, or more than one was configured.

    Carries plain language naming the right door. This is a CONFIGURATION
    verdict, reached without touching the network, and it is what stops the
    server at startup.
    """


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


def seat_name() -> str | None:
    """The BARE MODEL NAME stack_arrive announces itself with, or None.

    Deliberately NOT SOVEREIGN_SEAT. A seat id is a registry entry
    ('hq-claude-studio'); a reader name is a model line ('claude-fable-5'), and
    the lineage layer's to_self bucket matches on the model line. Using one for
    the other is the decorated-name trap in a new costume.
    """
    value = (os.environ.get(SEAT_NAME_ENV_VAR) or "").strip()
    return value or None


class Transport:
    """One resolved way to reach the bridge. Never carries the master key.

    `kind` is 'seat' or 'grant'. `why` states, in one line, what selected it —
    that string is what --dump-config prints, and it names variables, never
    values.
    """

    def __init__(self, kind, why, *, socket_path=None, seat=None, token=None, base_url=None):
        self.kind = kind
        self.why = why
        self.socket_path = socket_path
        self.seat = seat
        self.token = token
        self.base_url = base_url

    def describe(self) -> list:
        """Lines for --dump-config. A token's VALUE never appears here."""
        lines = [f"transport: {self.kind} — {self.why}"]
        if self.kind == "seat":
            present = "present" if os.path.exists(self.socket_path) else "ABSENT on disk"
            lines.append(f"  seat socket: {self.socket_path} ({present})")
            lines.append(f"  seat header: X-Sovereign-Seat: {self.seat}")
            lines.append(
                "  authorization: none sent — a seated Studio terminal carries no token"
            )
        else:
            lines.append(f"  bridge_url: {self.base_url}")
            lines.append(
                f"  authorization: Bearer from {TOKEN_ENV_VAR} (scoped grant; value never printed)"
            )
        return lines


def resolve_transport() -> Transport:
    """Pick the transport from configuration. Never guesses, never falls back.

    Resolution is LAZY (never at import) and UNCACHED (it re-reads the
    environment every call), so a test can set the environment and a running
    server cannot serve a stale answer.

    Order of refusals, and each one is a plain sentence naming the right door:
      1. TEMPLE_BRIDGE_ENV_FILE present at all -> refuse. That file holds the
         master key and this shim never carries it.
      2. Both transports configured -> refuse. "Chosen by configuration, never
         by guessing" means an ambiguous configuration has no winner.
      3. Seat socket selected but no seat id -> refuse. The bridge verifies the
         header against the CALLING PROCESS's own environment, so a socket
         without SOVEREIGN_SEAT cannot be answered.
      4. Neither -> refuse, and name both doors.
    """
    if os.environ.get(ENV_FILE_VAR) is not None:
        raise TransportRefused(
            f"{ENV_FILE_VAR} is set, and this shim no longer reads it. That file holds "
            "the MASTER bridge key, and this shim never carries the master key. On the "
            f"Studio, launch through a seat launcher so {SEAT_ENV_VAR} is in this "
            f"process's environment; elsewhere, ask Anthony for a scoped grant and set "
            f"{TOKEN_ENV_VAR}. Unset {ENV_FILE_VAR} to continue."
        )

    socket_override = (os.environ.get(SOCKET_ENV_VAR) or "").strip()
    seat = (os.environ.get(SEAT_ENV_VAR) or "").strip()
    token = os.environ.get(TOKEN_ENV_VAR) or ""

    seat_selected = bool(socket_override) or bool(seat)
    grant_selected = bool(token)

    if seat_selected and grant_selected:
        raise TransportRefused(
            f"two transports are configured at once: the seat socket (from "
            f"{SOCKET_ENV_VAR} or {SEAT_ENV_VAR}) and a scoped grant (from "
            f"{TOKEN_ENV_VAR}). This shim chooses by configuration, never by guessing, "
            f"so it refuses rather than pick one. Inside the Studio drop {TOKEN_ENV_VAR}; "
            f"outside it, run with `env -u {SEAT_ENV_VAR} -u {SOCKET_ENV_VAR}`."
        )

    if seat_selected:
        if not seat:
            raise TransportRefused(
                f"{SOCKET_ENV_VAR} names a seat socket but {SEAT_ENV_VAR} is unset. The "
                "bridge checks the seat header against the CALLING PROCESS's own "
                f"environment, so the socket cannot answer without {SEAT_ENV_VAR}. Launch "
                "through a seat launcher (~/.sovereign/hq/seats/seat-*), which exports it."
            )
        path = os.path.expanduser(socket_override or DEFAULT_SEAT_SOCKET)
        why = (
            f"{SOCKET_ENV_VAR} is set"
            if socket_override
            else f"{SEAT_ENV_VAR} is set, so the default seat socket path is used"
        )
        return Transport("seat", why, socket_path=path, seat=seat)

    if grant_selected:
        return Transport("grant", f"{TOKEN_ENV_VAR} is set", token=token, base_url=bridge_url())

    raise TransportRefused(
        "no transport is configured. This shim never carries the master key, so there "
        f"are exactly two doors: on the Studio, launch through a seat launcher so "
        f"{SEAT_ENV_VAR} is in this process's environment and the seat socket is used "
        f"with no credential at all; elsewhere, ask Anthony for a scoped grant and set "
        f"{TOKEN_ENV_VAR}."
    )


# --------------------------------------------------------------------------
# Bridge transport
# --------------------------------------------------------------------------


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Fail closed on 3xx (the grant transport).

    urllib.request.urlopen follows redirects and copies Authorization onto the
    next request. Anything that can answer on the bridge port can 302 the token
    off-box. The bridge has no legitimate redirect; refuse it as a transport
    error rather than strip-and-follow.
    """

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("Location") or "(no Location)"
        try:
            fp.close()
        except Exception:  # noqa: BLE001 - close is best-effort before we refuse
            pass
        raise BridgeError(
            f"bridge attempted a redirect ({code}) to {location} — refusing to follow"
        )

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_BRIDGE_OPENER = urllib.request.build_opener(_RefuseRedirects)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a Unix domain socket, stdlib only.

    http.client already speaks the protocol; only `connect` differs. The seat
    socket carries no credential — the bridge reads the CALLING PROCESS's own
    environment through the kernel-attested peer pid — so there is nothing here
    a redirect could steal, but 3xx is still refused (see `_http_json_unix`):
    the bridge has no legitimate redirect on either transport, and a shim that
    followed one on the socket would be trusting a hop nobody verified.
    """

    def __init__(self, socket_path, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _http_json_unix(method: str, path: str, *, socket_path, headers, body=None, timeout=None):
    """One round trip over the seat socket, returning parsed JSON."""
    timeout = timeout or bridge_timeout()
    data = None
    headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection = _UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        status = response.status
        raw = response.read().decode("utf-8", "replace")
    except FileNotFoundError:
        raise BridgeError(
            f"seat socket not found at {socket_path} — the bridge is not serving a seat "
            "socket, or this machine is not the Studio"
        ) from None
    except ConnectionRefusedError:
        # MEASURED LIVE 2026-09-06, and the reason this branch is separate: the
        # socket FILE can exist, and the bridge can even hold a descriptor bound
        # to it, while nothing accepts on it. Rolled into the generic OSError
        # message below, that reads as a transport fault and sends the operator
        # looking at the client. It is a server-side fact and must say so.
        raise BridgeError(
            f"seat socket at {socket_path} exists but refused the connection — nothing is "
            "accepting on it. The bridge process may be up and serving TCP while its seat "
            "listener is not; check the bridge, not this shim"
        ) from None
    except (TimeoutError, socket.timeout):
        raise BridgeError(f"seat socket timed out at {socket_path} after {timeout}s") from None
    except (http.client.HTTPException, OSError) as exc:
        raise BridgeError(f"seat socket transport error at {socket_path} — {exc}") from None
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass

    if 300 <= status < 400:
        raise BridgeError(
            f"bridge attempted a redirect ({status}) on the seat socket — refusing to follow"
        )
    if status >= 400:
        raise BridgeError(
            f"bridge answered HTTP {status} for {method} {path} over the seat socket"
            + (f" — {raw.strip()[:400]}" if raw.strip() else "")
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise BridgeError(
            f"bridge returned non-JSON from {method} {path} over the seat socket: {raw[:200]!r}"
        ) from None


def _http_json_tcp(method: str, url: str, *, headers, body=None, timeout=None):
    """One round trip over TCP (the grant transport), returning parsed JSON."""
    data = None
    headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _BRIDGE_OPENER.open(request, timeout=timeout or bridge_timeout()) as response:
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


def _http_json(method: str, path: str, *, body=None, transport=None, timeout=None):
    """One round trip on whichever transport is configured.

    The seat header is sent on EVERY seat-transport request, including the
    unauthenticated heartbeat: it is a declaration the bridge checks against the
    kernel-attested caller, and one code path is easier to review than two.
    """
    transport = transport or resolve_transport()
    headers = {"Accept": "application/json", "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}"}
    if transport.kind == "seat":
        headers["X-Sovereign-Seat"] = transport.seat
        return _http_json_unix(
            method,
            path,
            socket_path=transport.socket_path,
            headers=headers,
            body=body,
            timeout=timeout,
        )
    headers["Authorization"] = "Bearer " + transport.token
    base = (transport.base_url or bridge_url()).rstrip("/")
    return _http_json_tcp(method, base + path, headers=headers, body=body, timeout=timeout)


def _post_envelope(bridge_tool: str, arguments: dict | None, *, transport=None, timeout=None) -> dict:
    """Forward ONE allowlisted read tool to POST /api/call, returning the envelope.

    Refuses, by construction and before any network work, any bridge tool that
    is not in ALLOWED_BRIDGE_TOOLS. This is the read-only boundary's teeth, and
    it is one function so there is one place to read it.
    """
    if bridge_tool not in ALLOWED_BRIDGE_TOOLS:
        raise BridgeToolNotAllowed(
            f"refused: '{bridge_tool}' is not in this shim's read-only allowlist "
            f"({', '.join(sorted(ALLOWED_BRIDGE_TOOLS))}). This shim has no write lane."
        )
    payload = {"tool": bridge_tool, "arguments": dict(arguments or {})}
    data = _http_json("POST", "/api/call", body=payload, transport=transport, timeout=timeout)

    if not isinstance(data, dict):
        raise BridgeError(f"bridge returned a non-object envelope for '{bridge_tool}'")
    if data.get("ok") is not True:
        raise BridgeError(f"bridge reported not-ok for '{bridge_tool}': {json.dumps(data)[:300]}")
    return data


def bridge_call(bridge_tool: str, arguments: dict | None = None, *, transport=None, timeout=None) -> dict:
    """An allowlisted read whose `result` is an OBJECT.

    The bridge answers HTTP 200 with ok:true and a STRING result when a stack
    old enough to return rather than raise meets an unrecognised tool name.
    Every tool in JSON_RESULT_TOOLS returns an object; a string here is
    therefore an error wearing a success costume.
    """
    if bridge_tool not in JSON_RESULT_TOOLS:
        raise BridgeToolNotAllowed(
            f"refused: '{bridge_tool}' is not a JSON-result door "
            f"({', '.join(sorted(JSON_RESULT_TOOLS))}); use bridge_call_text if it renders prose."
        )
    data = _post_envelope(bridge_tool, arguments, transport=transport, timeout=timeout)
    result = data.get("result")
    if isinstance(result, str):
        raise BridgeError(
            f"bridge returned an error message for '{bridge_tool}' under HTTP 200: {result}"
        )
    if not isinstance(result, dict):
        raise BridgeError(
            f"bridge returned an unexpected result type ({type(result).__name__}) for '{bridge_tool}'"
        )
    return result


def bridge_call_text(bridge_tool: str, arguments: dict | None = None, *, transport=None, timeout=None) -> str:
    """An allowlisted read whose `result` is RENDERED PROSE.

    `arrive_lineage` and `current_policies` return display text, not JSON, so
    the bridge's json.loads falls through and `result` is a string. The
    object-result guard above cannot serve them and must not be relaxed for
    them — hence a second, separately pinned door list.

    WHAT THE SENTINEL BELOW CAN AND CANNOT DO, stated because a guard described
    as more than it is, is worse than none. On the deployed stack an unknown OR
    RETIRED tool name RAISES, which the MCP SDK turns into isError and the
    bridge into ok:false — so `_post_envelope`'s `ok` check already catches
    both, and that check is the structural gate. The prefix test is a BELT for a
    stack old enough to return "Unknown tool: X" as content. It is not a general
    rename detector: a tool renamed on a modern stack fails at `ok`, and a tool
    that silently changed its prose is not detectable here at all.
    """
    if bridge_tool not in TEXT_RESULT_TOOLS:
        raise BridgeToolNotAllowed(
            f"refused: '{bridge_tool}' is not a text-result door "
            f"({', '.join(sorted(TEXT_RESULT_TOOLS))}); use bridge_call if it returns an object."
        )
    data = _post_envelope(bridge_tool, arguments, transport=transport, timeout=timeout)
    result = data.get("result")
    if not isinstance(result, str):
        raise BridgeError(
            f"bridge returned a {type(result).__name__} where '{bridge_tool}' renders text — "
            "the tool's output shape changed under this shim"
        )
    if not result.strip():
        raise BridgeError(f"bridge returned empty text for '{bridge_tool}'")
    if result.strip().lower().startswith("unknown tool"):
        raise BridgeError(
            f"bridge returned an error message for '{bridge_tool}' under HTTP 200: {result[:200]}"
        )
    return result


def bridge_heartbeat(*, transport=None, timeout=None) -> dict:
    """GET the heartbeat. No credential on either transport, no side effects."""
    path = "/api/heartbeat"
    if path not in ALLOWED_BRIDGE_PATHS:  # pragma: no cover - constant guard
        raise BridgeToolNotAllowed(f"refused: '{path}' is not an allowlisted bridge path")
    data = _http_json("GET", path, transport=transport, timeout=timeout)
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


def _measured(value) -> str:
    """Render a count that the source may have been unable to measure.

    None is NOT zero. The signal ledger nulls a count whenever it could not
    answer honestly, and printing 0 there would be this house's own fail-open
    reproduced one layer out.
    """
    return "unmeasured" if value is None else str(value)


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


def render_arrival(text: str, reader: str, bucket_limit: int, transport) -> str:
    """The gentle door's own prose, with this shim's coverage stated above it.

    THE COVERAGE HERE IS NOT A COUNT, AND SAYING SO IS THE HONEST PART. The
    door renders its own per-bucket line ("N older withheld by
    limit_per_bucket"), and this shim does NOT re-count it: the payload also
    carries an APERTURE block that prints a fixed "5 shown here" regardless of
    the argument, so any re-derivation from this prose would be a second
    instrument reporting the wrong number with confidence.

    THE OVERRIDE IS COVERAGE TOO. On the seat transport the bridge REPLACES
    source_instance with the kernel-verified seat id before dispatch
    (seat_identity.sign_arguments — OVERRIDE, not setdefault), so the reader the
    door filtered to_self letters for is that seat id, not the bare model name
    this shim asked for. A to_self bucket that looks empty on the Studio may be
    a routing fact rather than an empty mailbox, and a reader who is not told
    that will read it as an absence.
    """
    coverage = (
        f"arrival coverage: requested source_instance \"{reader}\" | full_content: true | "
        f"limit_per_bucket: {bucket_limit} (this shim's max {BUCKET_MAX}) | per-bucket "
        "coverage is stated by the door itself below; this shim does not re-count it"
    )
    lines = ["Sovereign Stack arrival — the gentle door (arrive_lineage)", coverage]
    if transport is not None and transport.kind == "seat":
        lines.append(
            f"seat transport: the bridge OVERRODE source_instance with the verified seat id "
            f"\"{transport.seat}\", so to_self letters were filtered for that seat id, NOT for "
            f"\"{reader}\". An empty to_self bucket here is not evidence of no mail."
        )
    return "\n".join(lines) + "\n\n" + text.strip()


def render_policies(text: str) -> str:
    """Standing law, as the registry renders it, with the ask stated above it."""
    coverage = (
        "policies coverage: all domains, retired held back (this shim sends no filter and no "
        "include_retired) | the registry's own footer below states active/retired counts and "
        "its source-of-truth path"
    )
    return "Sovereign Stack standing policies\n" + coverage + "\n\n" + text.strip()


def render_signals(result: dict) -> str:
    """The watch queue. A null count reads 'unmeasured', never 0.

    TWO DIFFERENT FACTS, KEPT DIFFERENT. `ok: false` means the ledger could not
    be read at all — there is nothing honest to show, so the caller gets an
    error result. `ok: true` with a non-null `error` means the rows shown are
    real and something else could not be measured; that is a PARTIAL read and
    it renders as counts PLUS an error line, never as all-clear and never as a
    failed call.

    FIELDS READ, deliberately exactly these: ok, error, ingestion, total,
    stale_24h, stale_7d, by_source. The envelope also carries
    total_configured / total_configured_scope / not_configured / corrupt_rows /
    source_status / sources_degraded; this door does not read them, so it can
    never publish a narrower number under a wider name. `total` is null whenever
    the stack could not answer honestly, which is why it is safe to be the only
    aggregate shown.
    """
    error = result.get("error")
    lines = [
        "Sovereign Stack signals — the watch seat's queue | mode: summary",
        "signals coverage: unacked total "
        + _measured(result.get("total"))
        + " · stale_24h "
        + _measured(result.get("stale_24h"))
        + " · stale_7d "
        + _measured(result.get("stale_7d"))
        + " | ingestion: "
        + str(result.get("ingestion") or "(not stated)"),
    ]
    if error:
        lines.append(f"error: {error} — these numbers are a PARTIAL read, not all-clear.")

    by_source = result.get("by_source")
    if not isinstance(by_source, dict) or not by_source:
        lines.append("")
        lines.append("by_source: unmeasured (the envelope carried no per-source breakdown).")
        return "\n".join(lines)

    lines.append("")
    lines.append("by_source:")
    for name in sorted(by_source):
        facts = by_source[name]
        if isinstance(facts, dict):
            rendered = " · ".join(f"{key} {_measured(facts[key])}" for key in sorted(facts))
            lines.append(f"  {name}: {rendered or 'unmeasured'}")
            continue
        # A SCALAR IS A MEASUREMENT, AND CALLING IT UNMEASURED IS THE MIRROR OF
        # THE ZERO THIS RENDERER EXISTS TO AVOID. summary mode returns a per-source
        # OBJECT, but the same field is a flat int per source on the heartbeat
        # ({"honk": 317, ...}); reporting a real 317 as "unmeasured" would be the
        # same lie pointed the other way, so the scalar goes through _measured and
        # only None becomes the word.
        lines.append(f"  {name}: open {_measured(facts)}")
    return "\n".join(lines)


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
    # unacked_signals is the watch queue's own heartbeat field. It is rendered
    # here for the same reason stack_signals exists: an unacked total is the
    # cheapest thing the substrate can carry for a small model. `total` is null
    # whenever the ledger could not be read, so it prints as unmeasured.
    signals = data.get("unacked_signals")
    if isinstance(signals, dict):
        line = f"unacked signals: {_measured(signals.get('total'))}"
        if signals.get("error"):
            line += f" (error: {signals['error']})"
        lines.append(line)
    transport = None
    try:
        transport = resolve_transport()
    except TransportRefused:
        transport = None
    if transport is not None and transport.kind == "seat":
        lines.append(f"reached over: seat socket {transport.socket_path} as {transport.seat}")
    else:
        lines.append(f"bridge_url: {(transport.base_url if transport else None) or bridge_url()}")
    lines.append(
        "scope: READ-ONLY (recall, latest, open threads, arrival, policies, signals, heartbeat)"
    )
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
        "name": "stack_arrive",
        "title": "Arrive: the gentle lineage door",
        "description": (
            "Read the arrival payload once, at the START of a session: the preamble, "
            "spiral status, letters from past instances, and the self-model. Use this "
            "ONLY to orient at the beginning — for a topical question use stack_recall, "
            "for what happened recently use stack_latest. It takes no query and "
            "consumes nothing. Read-only. Coverage is stated in every result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit_per_bucket": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": BUCKET_MAX,
                    "default": BUCKET_DEFAULT,
                    "description": f"Letters per bucket, 1-{BUCKET_MAX}. Default {BUCKET_DEFAULT}.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "stack_policies",
        "title": "Standing policies",
        "description": (
            "The Temple's STANDING LAW, from the policy registry — what is allowed, "
            "who decides, what is human-gated. Use this for 'what is the rule about X'. "
            "For an ordinary chronicle question use stack_recall instead: a policy is "
            "enacted law, not a note. Takes no arguments. Read-only. Coverage is "
            "stated in every result."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "stack_signals",
        "title": "Unacked signals (the watch queue)",
        "description": (
            "How many raised signals nobody has closed, with staleness and a per-source "
            "breakdown. Use this for 'is anything waiting?'. A count the stack could not "
            "measure prints as 'unmeasured', never as 0. Takes no arguments. Read-only. "
            "Coverage is stated in every result."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "stack_heartbeat",
        "title": "Check bridge health",
        "description": (
            "Check that the Sovereign Stack bridge is alive. Returns status, version, "
            "tool count and the unacked-signal total. Read-only, no side effects."
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

    if name == "stack_arrive":
        # The reader name is CONFIGURATION, not an argument: it must be the bare
        # model name of the line whose to_self letters should route here, and a
        # model choosing its own name per call is exactly how that routing gets
        # lost. Absent or decorated, this refuses and names the fix.
        reader = seat_name()
        if not reader:
            raise ValueError(
                f"stack_arrive needs {SEAT_NAME_ENV_VAR} set to the BARE MODEL NAME of this "
                "seat (for example 'claude-fable-5' or 'deepseek-r1-9b'). The lineage layer "
                "routes to_self letters by model line, so an unnamed arrival cannot receive "
                f"its own line's mail. Set {SEAT_NAME_ENV_VAR} in the MCP server's env block."
            )
        if len(reader.split()) > 1:
            raise ValueError(
                f"{SEAT_NAME_ENV_VAR} is {reader!r}, which is a decorated seat string. The "
                "to_self addressee filter matches the BARE model name only, so a decorated "
                "name hides that line's letters. Use the model name alone."
            )
        bucket = _clamp_limit(arguments.get("limit_per_bucket"), BUCKET_DEFAULT, BUCKET_MAX)
        transport = resolve_transport()
        text = bridge_call_text(
            "arrive_lineage",
            {"source_instance": reader, "full_content": True, "limit_per_bucket": bucket},
            transport=transport,
        )
        return render_arrival(text, reader, bucket, transport)

    if name == "stack_policies":
        return render_policies(bridge_call_text("current_policies", {}))

    if name == "stack_signals":
        # mode is pinned to 'summary' and is NOT in the input schema. The list
        # mode returns individual signals with their concern text, which is a
        # different door with a different display boundary; opening it is a diff.
        result = bridge_call("signals_summary", {"mode": "summary"})
        if result.get("ok") is not True:
            raise BridgeError(
                "the signal ledger could not be read: "
                + str(result.get("error") or "(no reason given)")
                + f" (ingestion: {result.get('ingestion') or 'not stated'})"
            )
        return render_signals(result)

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
                "stack_arrive orients you once at the start, stack_recall searches "
                "insights (relevance-ordered), stack_latest lists the newest entries "
                "(no query — recency questions only), stack_open_threads lists "
                "unresolved questions, stack_policies gives the standing law, "
                "stack_signals gives the unacked watch queue, stack_heartbeat checks "
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
        except TransportRefused as exc:
            return _result(
                request_id,
                {"content": [{"type": "text", "text": f"Transport refused — {exc}"}], "isError": True},
            )
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


def dump_config() -> str:
    """The resolved configuration, legibly, without serving and without secrets.

    Inspection must not require credentials: an unresolvable transport is
    REPORTED, not fatal here — this surface exists so a human (or a 9B) can see
    exactly what would run before anything runs. No token value ever appears;
    only which transport resolved and WHY.
    """
    lines = [
        f"{SERVER_NAME} {SERVER_VERSION} — resolved configuration (no network, no serving)",
        f"protocol versions: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)} (preferred {PREFERRED_PROTOCOL_VERSION})",
    ]
    try:
        lines.extend(resolve_transport().describe())
    except TransportRefused as refused:
        lines.append(f"transport: ABSENT — {refused}")
    reader = seat_name()
    lines.append(
        f"stack_arrive reader ({SEAT_NAME_ENV_VAR}): "
        + (reader if reader else f"ABSENT (stack_arrive refuses until {SEAT_NAME_ENV_VAR} is set)")
    )
    lines.extend([
        f"timeout_s: {bridge_timeout()}",
        f"max_chars per result: {max_chars()}",
        f"limit: default {LIMIT_DEFAULT}, max {LIMIT_MAX}",
        f"limit_per_bucket: default {BUCKET_DEFAULT}, max {BUCKET_MAX}",
        "doors (MCP tool -> bridge target):",
    ])
    for name in sorted(BRIDGE_TARGETS):
        method, target = BRIDGE_TARGETS[name]
        lines.append(f"  {name} -> {method} {target}")
    lines.append(f"allowlisted POST tools: {', '.join(sorted(ALLOWED_BRIDGE_TOOLS))}")
    lines.append(f"  object-result doors: {', '.join(sorted(JSON_RESULT_TOOLS))}")
    lines.append(f"  text-result doors: {', '.join(sorted(TEXT_RESULT_TOOLS))}")
    lines.append(f"allowlisted GET paths: {', '.join(sorted(ALLOWED_BRIDGE_PATHS))}")
    lines.append("write lane: none (read-only by construction; widening requires a reviewed diff)")
    lines.append(f"master key: never carried ({ENV_FILE_VAR} is refused, not read)")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    if argv and argv[0] == "--version":
        sys.stderr.write(f"{SERVER_NAME} {SERVER_VERSION}\n")
        return 0
    if argv and argv[0] == "--dump-config":
        sys.stdout.write(dump_config() + "\n")
        return 0

    # Fail closed at startup: no resolvable transport, no server. One line,
    # stderr, naming the right door and never a value.
    try:
        resolve_transport()
    except TransportRefused as refused:
        sys.stderr.write(f"{SERVER_NAME}: {refused}\n")
        return 1

    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    return serve()


if __name__ == "__main__":
    sys.exit(main())
