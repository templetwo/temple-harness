#!/usr/bin/env python3
"""Tests for the Temple Stack MCP shim.

Stdlib unittest only. These tests NEVER touch the real bridge, NEVER need a real
credential, and NEVER read the master key's env file — that file is refused by
the shim now and the refusal has its own tests.

TWO FAKE BRIDGES, ONE PER TRANSPORT, because the shim has two:
  * TCP on a random localhost port, driven by TEMPLE_BRIDGE_URL +
    TEMPLE_BRIDGE_TOKEN — the SCOPED GRANT transport.
  * A Unix domain socket in a temp dir, driven by TEMPLE_BRIDGE_SOCKET +
    SOVEREIGN_SEAT — the SEAT SOCKET transport. Same handler, so the two paths
    are compared against one server behaviour rather than two fixtures.

Run:  python3 -m unittest discover -s tests -v      (from mcp-shim/)
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM_PATH = os.path.join(os.path.dirname(HERE), "temple_stack_mcp.py")

# A dummy SCOPED GRANT value. The master key is never involved: the shim refuses
# to load it, and nothing in this suite reads or needs any real value.
DUMMY_CREDENTIAL = "not-a-real-token"
# A value the fake bridge deliberately rejects, so the HTTP 401 path is exercised.
REJECTED_CREDENTIAL = "rejected-by-the-fake-bridge"
# The seat id the fake seat socket expects to see in X-Sovereign-Seat.
DUMMY_SEAT = "test-seat-studio"
# The BARE MODEL NAME stack_arrive announces itself with (TEMPLE_SEAT_NAME).
DUMMY_SEAT_NAME = "test-model-9b"
# A distinctive string the dump-config tests assert never reaches stdout/stderr.
SENTINEL_VALUE = "sentinel-value-that-must-never-print"

# Every environment variable the shim resolves a transport from. Popped before
# every spawn so an ambient SOVEREIGN_SEAT in the developer's own terminal
# cannot make the suite refuse for transport-ambiguity reasons.
TRANSPORT_ENV_VARS = (
    "TEMPLE_BRIDGE_TOKEN",
    "TEMPLE_BRIDGE_SOCKET",
    "TEMPLE_BRIDGE_ENV_FILE",
    "SOVEREIGN_SEAT",
    "TEMPLE_SEAT_NAME",
    "TEMPLE_BRIDGE_URL",
)


def clean_env(**overrides):
    """os.environ minus every transport variable, plus the ones asked for."""
    env = dict(os.environ)
    for name in TRANSPORT_ENV_VARS:
        env.pop(name, None)
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


def grant_transport(base_url, value=DUMMY_CREDENTIAL):
    """A grant Transport for the direct-call tests, built without the environment."""
    return shim.Transport("grant", "test fixture", token=value, base_url=base_url)


def seat_transport(socket_path, seat=DUMMY_SEAT):
    """A seat Transport for the direct-call tests, built without the environment."""
    return shim.Transport("seat", "test fixture", socket_path=socket_path, seat=seat)


def load_shim_module():
    """Import the shim by absolute path.

    Import must NOT exit even with no token present — token resolution is lazy,
    inside main()/load_token(), precisely so this import is safe.
    """
    spec = importlib.util.spec_from_file_location("temple_stack_mcp_under_test", SHIM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shim = load_shim_module()


# ---------------------------------------------------------------------------
# Fake bridge
# ---------------------------------------------------------------------------


class FakeBridgeState:
    """Shared, mutable state for the fake bridge: what it returns, what it saw."""

    def __init__(self):
        self.calls = []           # every parsed POST /api/call body, in order
        self.headers = []         # every request's headers, in order, as dicts
        self.result_payload = None  # dict OR str -> wrapped in {"ok":true,"result":...}
        # Per-bridge-tool payloads, so ONE fake can serve doors whose result
        # types differ: recall returns an object, current_policies returns prose.
        self.payload_by_tool = {}
        self.raw_response = None    # str -> returned verbatim (fail-open shapes)
        self.heartbeat = {
            "status": "ok",
            "version": "1.15.0-fake",
            "tools": 52,
            "source_commit": "deadbee",
            "unacked_signals": {"total": 3, "error": None, "ingestion": "fresh"},
        }

    def reset(self):
        self.calls.clear()
        self.headers.clear()
        self.result_payload = None
        self.payload_by_tool = {}
        self.raw_response = None

    def header_of(self, name, index=-1):
        """One header from a recorded request. None when it was not sent."""
        return self.headers[index].get(name) if self.headers else None


STATE = FakeBridgeState()


class FakeBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence the test log
        pass

    def _send(self, code, body: str):
        encoded = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _record_headers(self):
        STATE.headers.append({key: value for key, value in self.headers.items()})

    def do_GET(self):
        self._record_headers()
        if self.path == "/api/heartbeat":
            self._send(200, json.dumps(STATE.heartbeat))
        else:
            self._send(404, json.dumps({"detail": "not found"}))

    def do_POST(self):
        self._record_headers()
        if self.path != "/api/call":
            self._send(404, json.dumps({"detail": "not found"}))
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"_unparseable": raw}
        STATE.calls.append(parsed)

        # THE SEAT PATH CARRIES NO CREDENTIAL, and this fake enforces that the
        # same way the real bridge does: an Authorization header of any kind
        # routes to the bearer check, and only a request with none at all may
        # present a seat header. A shim that sent both would fail here.
        authorization = self.headers.get("Authorization")
        seat = self.headers.get("X-Sovereign-Seat")
        if authorization is None and seat:
            if seat != DUMMY_SEAT:
                self._send(401, json.dumps({"detail": f"seat {seat} is not registered", "failure_class": "auth"}))
                return
        elif not (authorization or "").startswith("Bearer ") or (authorization or "").endswith(REJECTED_CREDENTIAL):
            self._send(401, json.dumps({"detail": "Missing or malformed Bearer token.", "failure_class": "auth"}))
            return

        if STATE.raw_response is not None:
            self._send(200, STATE.raw_response)
            return
        tool = parsed.get("tool") if isinstance(parsed, dict) else None
        if tool in STATE.payload_by_tool:
            payload = STATE.payload_by_tool[tool]
        elif STATE.result_payload is not None:
            payload = STATE.result_payload
        else:
            payload = default_recall_result()
        self._send(200, json.dumps({"ok": True, "result": payload, "duration_ms": 1}))


def default_recall_result(items=None, total=786):
    return {
        "items": items if items is not None else [
            {
                "timestamp": "2026-08-24T01:32:32.492869+00:00",
                "domain": "hq-ops",
                "content": "A chronicle entry body.",
                "intensity": 0.5,
                "layer": "ground_truth",
                "session_id": "spiral_test",
                "verified_by": [{"kind": "cmd", "ref": "echo hi"}],
                "claim_id": "claim_test_1",
            }
        ],
        "returned": 1,
        "total_matched": total,
        "offset": 0,
        "scope": {"mode": "all", "domain_query": None, "domains_searched": 1186, "domains_total": 1186},
        "truncated": True,
        "partial_reasons": [f"truncated:{total}"],
        "continuation": {"offset": 1, "limit": 1},
    }


def default_threads_result():
    return {
        "items": [
            {
                "timestamp": "2026-08-17T19:01:15.809497+00:00",
                "thread_id": "thread_20260817_190115_5b4eadd6",
                "question": "Does the rollback drill keep the restored instance blind?",
                "context": "Frozen note lives off-box.",
                "domain": "grok-mesh",
                "resolved": False,
                "touch_count": 2,
            }
        ],
        "returned": 1,
        "total_matched": 170,
        "offset": 0,
        "truncated": True,
        "partial_reasons": ["truncated:170"],
        "continuation": {"offset": 1, "limit": 1},
    }


def default_signals_summary(total=3, stale_24h=1, stale_7d=0, error=None):
    """What signals_summary(mode='summary') returns, in the stack's own shape.

    Nulls are load-bearing here: the stack nulls a count it could not measure
    rather than publishing a zero, and the renderer must carry that through.
    """
    return {
        "ok": True,
        "mode": "summary",
        "error": error,
        "ingestion": "fresh",
        "scanned_at": "2026-09-06T20:00:00+00:00",
        "total": total,
        "total_configured": total,
        "total_configured_scope": ["honk", "guardian"],
        "not_configured": [],
        "stale_24h": stale_24h,
        "stale_7d": stale_7d,
        "by_source": {
            "honk": {"open": 2, "stale_24h": 1, "stale_7d": 0, "oldest_unacked": "2026-09-05T10:00:00+00:00"},
            "guardian": {"open": None, "stale_24h": None, "stale_7d": None, "oldest_unacked": None},
        },
        "corrupt_rows": 0,
        "source_status": {"honk": "ok", "guardian": "unavailable"},
        "sources_degraded": ["guardian"],
    }


# The two TEXT-returning doors. The bridge json.loads a tool's TextContent and
# falls back to the raw string, so these arrive as `result` STRINGS.
FAKE_ARRIVAL_TEXT = (
    "\u2748 ARRIVE_LINEAGE \u2014 relational arrival\n\n"
    "\u2501\u2501\u2501 SPIRAL STATUS \u2501\u2501\u2501\n  Session: spiral_test\n\n"
    "\u2501\u2501\u2501 COMMS \u2014 LINEAGE \u2501\u2501\u2501\n"
    "  to_arrival: showing 5 of 13 \u2014 8 older withheld by limit_per_bucket\n"
)
FAKE_POLICIES_TEXT = (
    "\U0001f4dc Standing policies \u2014 13 active\n\n"
    "  pol_20260804_delegation-tier-law \u2014 active\n\n---\n"
    "Source of truth: /fake/policies.jsonl (append-only; latest record per policy_id wins).\n"
    "13 active \u00b7 0 retired.\n"
)


# ---------------------------------------------------------------------------
# Fake SEAT SOCKET bridge (the second transport)
# ---------------------------------------------------------------------------


class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    """The same handler, served over AF_UNIX.

    BaseHTTPRequestHandler expects `client_address` to be a (host, port) tuple;
    an AF_UNIX accept returns '' for it. Substituting a tuple here is the whole
    adaptation — nothing about the HTTP behaviour changes, so the two transports
    are tested against ONE server behaviour rather than two fixtures that could
    drift apart.
    """

    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("127.0.0.1", 0)


class SeatSocketBridge:
    """A fake seat socket in a temp dir. Started per test class, torn down after."""

    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="temple-seat-sock-")
        self.path = os.path.join(self.directory, "bridge.sock")
        self.server = _UnixHTTPServer(self.path, FakeBridgeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Subprocess harness
# ---------------------------------------------------------------------------


class ShimProcess:
    """Spawn the shim as a real subprocess and speak newline-delimited JSON-RPC.

    Transport is chosen the way the shim chooses it — by environment, never by
    an argument the shim cannot see. `socket_path` selects the seat transport
    (and then nothing selecting the grant transport is set at all); otherwise
    the grant transport is used.
    """

    def __init__(
        self,
        base_url=None,
        *,
        max_chars=None,
        value=DUMMY_CREDENTIAL,
        socket_path=None,
        seat=DUMMY_SEAT,
        seat_name=DUMMY_SEAT_NAME,
        env_file=None,
        extra_env=None,
    ):
        env = clean_env()
        env["TEMPLE_MCP_TIMEOUT"] = "10"
        if socket_path is not None:
            env["TEMPLE_BRIDGE_SOCKET"] = socket_path
            if seat is not None:
                env["SOVEREIGN_SEAT"] = seat
        else:
            if base_url is not None:
                env["TEMPLE_BRIDGE_URL"] = base_url
            if value is not None:
                env["TEMPLE_BRIDGE_TOKEN"] = value
        if seat_name is not None:
            env["TEMPLE_SEAT_NAME"] = seat_name
        if max_chars is not None:
            env["TEMPLE_MCP_MAX_CHARS"] = str(max_chars)
        if env_file is not None:
            env["TEMPLE_BRIDGE_ENV_FILE"] = env_file
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, SHIM_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout=15):
        holder = {}

        def _read():
            holder["line"] = self.proc.stdout.readline()

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise AssertionError("timed out waiting for a response line from the shim")
        line = holder.get("line")
        if not line:
            raise AssertionError(f"shim closed stdout unexpectedly; stderr={self.proc.stderr.read()!r}")
        return json.loads(line)

    def request(self, message):
        self.send(message)
        return self.read()

    def handshake(self, protocol_version="2025-06-18"):
        response = self.request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def close(self):
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


class ShimTestCase(unittest.TestCase):
    """Base: one fake bridge for the whole class, one shim process per test."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridgeHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        STATE.reset()
        self.shims = []

    def tearDown(self):
        for shim_proc in self.shims:
            shim_proc.close()

    def spawn(self, **kwargs):
        kwargs.setdefault("base_url", self.base_url)
        proc = ShimProcess(**kwargs)
        self.shims.append(proc)
        return proc

    @staticmethod
    def text_of(response):
        content = response["result"]["content"]
        return "".join(part["text"] for part in content if part.get("type") == "text")


# ---------------------------------------------------------------------------
# Handshake / protocol
# ---------------------------------------------------------------------------


class TestHandshake(ShimTestCase):
    def test_initialize_echoes_supported_client_version(self):
        proc = self.spawn()
        response = proc.handshake("2025-06-18")
        result = response["result"]
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "temple-stack")
        self.assertIn("tools", result["capabilities"])

    def test_initialize_accepts_older_supported_version(self):
        proc = self.spawn()
        result = proc.handshake("2024-11-05")["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")

    def test_initialize_falls_back_on_unknown_version(self):
        proc = self.spawn()
        result = proc.handshake("1999-01-01")["result"]
        self.assertEqual(result["protocolVersion"], shim.PREFERRED_PROTOCOL_VERSION)

    def test_notification_gets_no_response_and_server_survives(self):
        proc = self.spawn()
        proc.handshake()
        proc.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}})
        pong = proc.request({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        self.assertEqual(pong["id"], 7)
        self.assertEqual(pong["result"], {})

    def test_unknown_method_returns_method_not_found(self):
        proc = self.spawn()
        proc.handshake()
        response = proc.request({"jsonrpc": "2.0", "id": 8, "method": "resources/list"})
        self.assertEqual(response["error"]["code"], -32601)
        pong = proc.request({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(pong["id"], 9)

    def test_invalid_json_returns_parse_error_without_crashing(self):
        proc = self.spawn()
        proc.handshake()
        proc.proc.stdin.write("{this is not json\n")
        proc.proc.stdin.flush()
        response = proc.read()
        self.assertEqual(response["error"]["code"], -32700)
        pong = proc.request({"jsonrpc": "2.0", "id": 10, "method": "ping"})
        self.assertEqual(pong["id"], 10)


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


class TestToolsList(ShimTestCase):
    def _tools(self):
        proc = self.spawn()
        proc.handshake()
        return proc.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]

    def test_exactly_seven_tools(self):
        tools = self._tools()
        self.assertEqual(len(tools), 7)
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "stack_recall", "stack_latest", "stack_open_threads",
                "stack_arrive", "stack_policies", "stack_signals", "stack_heartbeat",
            },
        )

    def test_schemas(self):
        by_name = {tool["name"]: tool for tool in self._tools()}

        recall = by_name["stack_recall"]["inputSchema"]
        self.assertEqual(recall["type"], "object")
        self.assertEqual(recall["required"], ["query"])
        self.assertFalse(recall["additionalProperties"])
        self.assertEqual(set(recall["properties"]), {"query", "domain", "limit"})
        self.assertEqual(recall["properties"]["limit"]["maximum"], 10)
        self.assertEqual(recall["properties"]["limit"]["default"], 5)
        # order must NOT be exposed: relevance is pinned, not negotiable.
        self.assertNotIn("order", recall["properties"])

        latest = by_name["stack_latest"]["inputSchema"]
        self.assertEqual(latest["type"], "object")
        self.assertEqual(latest["required"], [])
        self.assertFalse(latest["additionalProperties"])
        # query must NOT exist on this door: recency reads carry no search terms.
        self.assertEqual(set(latest["properties"]), {"domain", "limit"})
        self.assertNotIn("query", latest["properties"])
        self.assertNotIn("order", latest["properties"])
        self.assertEqual(latest["properties"]["limit"]["maximum"], 10)
        self.assertEqual(latest["properties"]["limit"]["default"], 5)

        threads = by_name["stack_open_threads"]["inputSchema"]
        self.assertEqual(threads["required"], [])
        self.assertEqual(set(threads["properties"]), {"limit"})
        self.assertEqual(threads["properties"]["limit"]["maximum"], 10)

        heartbeat = by_name["stack_heartbeat"]["inputSchema"]
        self.assertEqual(heartbeat["properties"], {})
        self.assertFalse(heartbeat["additionalProperties"])

        arrive = by_name["stack_arrive"]["inputSchema"]
        self.assertEqual(arrive["required"], [])
        self.assertFalse(arrive["additionalProperties"])
        # The reader name is CONFIGURATION (TEMPLE_SEAT_NAME), never an argument:
        # a model choosing its own name per call is how to_self routing is lost.
        self.assertEqual(set(arrive["properties"]), {"limit_per_bucket"})
        self.assertNotIn("source_instance", arrive["properties"])
        self.assertNotIn("full_content", arrive["properties"])
        self.assertEqual(arrive["properties"]["limit_per_bucket"]["maximum"], shim.BUCKET_MAX)
        self.assertEqual(arrive["properties"]["limit_per_bucket"]["default"], shim.BUCKET_DEFAULT)

        # Both no-argument doors: mode / domain / include_retired are pinned
        # OUTSIDE the schema exactly the way `order` is on the recall doors.
        for name in ("stack_policies", "stack_signals"):
            schema = by_name[name]["inputSchema"]
            self.assertEqual(schema["properties"], {}, name)
            self.assertEqual(schema["required"], [], name)
            self.assertFalse(schema["additionalProperties"], name)

    def test_every_tool_has_a_description(self):
        for tool in self._tools():
            self.assertTrue(tool.get("description"), f"{tool['name']} has no description")


# ---------------------------------------------------------------------------
# tools/call against the fake bridge
# ---------------------------------------------------------------------------


class TestToolCalls(ShimTestCase):
    def call(self, proc, name, arguments=None, request_id=3):
        return proc.request({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def test_recall_always_forwards_order_relevance(self):
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_recall", {"query": "flock race"})
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(len(STATE.calls), 1)
        body = STATE.calls[0]
        self.assertEqual(body["tool"], "recall_insights")
        self.assertEqual(body["arguments"]["order"], "relevance")
        self.assertEqual(body["arguments"]["query"], "flock race")
        self.assertEqual(body["arguments"]["limit"], 5)
        self.assertNotIn("domain", body["arguments"])

    def test_recall_order_cannot_be_overridden_by_the_caller(self):
        proc = self.spawn()
        proc.handshake()
        self.call(proc, "stack_recall", {"query": "x", "order": "newest"})
        self.assertEqual(STATE.calls[0]["arguments"]["order"], "relevance")

    def test_recall_forwards_domain_and_clamps_limit(self):
        proc = self.spawn()
        proc.handshake()
        self.call(proc, "stack_recall", {"query": "x", "domain": "hq-ops", "limit": 99})
        arguments = STATE.calls[0]["arguments"]
        self.assertEqual(arguments["domain"], "hq-ops")
        self.assertEqual(arguments["limit"], 10)

    def test_recall_renders_content_and_bridge_coverage(self):
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_recall", {"query": "flock"}))
        self.assertIn("A chronicle entry body.", text)
        self.assertIn("hq-ops", text)
        self.assertIn("returned 1 of 786 matched", text)
        self.assertIn("order: relevance", text)

    def test_recall_empty_result_says_so(self):
        STATE.result_payload = default_recall_result(items=[], total=0)
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_recall", {"query": "nothing matches this"})
        self.assertFalse(response["result"]["isError"])
        self.assertIn("No matching chronicle entries.", self.text_of(response))

    def test_recall_rejects_empty_query(self):
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_recall", {"query": "   "})
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(STATE.calls, [])

    def test_latest_forwards_order_newest_and_no_query(self):
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_latest", {})
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(len(STATE.calls), 1)
        body = STATE.calls[0]
        self.assertEqual(body["tool"], "recall_insights")
        self.assertEqual(body["arguments"]["order"], "newest")
        self.assertEqual(body["arguments"]["limit"], 5)
        # A tail read carries no search terms — ever.
        self.assertNotIn("query", body["arguments"])
        self.assertNotIn("domain", body["arguments"])

    def test_latest_order_cannot_be_overridden_by_the_caller(self):
        proc = self.spawn()
        proc.handshake()
        self.call(proc, "stack_latest", {"order": "relevance"})
        self.assertEqual(STATE.calls[0]["arguments"]["order"], "newest")

    def test_latest_refuses_a_query_and_redirects_to_recall(self):
        # The groove-guard: a query here means the caller wanted stack_recall.
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_latest", {"query": "num_ctx"})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("stack_recall", self.text_of(response))
        # Refused before any bridge work, same as every other boundary here.
        self.assertEqual(STATE.calls, [])

    def test_latest_forwards_domain_and_clamps_limit(self):
        proc = self.spawn()
        proc.handshake()
        self.call(proc, "stack_latest", {"domain": "sovereign-data", "limit": 99})
        arguments = STATE.calls[0]["arguments"]
        self.assertEqual(arguments["domain"], "sovereign-data")
        self.assertEqual(arguments["limit"], 10)

    def test_latest_renders_header_and_bridge_coverage(self):
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_latest", {}))
        self.assertIn("Sovereign Stack latest", text)
        self.assertIn("order: newest", text)
        self.assertIn("A chronicle entry body.", text)
        self.assertIn("returned 1 of 786 matched", text)

    def test_latest_empty_chronicle_says_so(self):
        STATE.result_payload = default_recall_result(items=[], total=0)
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_latest", {})
        self.assertFalse(response["result"]["isError"])
        self.assertIn("No chronicle entries.", self.text_of(response))

    def test_open_threads_forwards_and_renders(self):
        STATE.result_payload = default_threads_result()
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_open_threads", {"limit": 3}))
        self.assertEqual(STATE.calls[0]["tool"], "get_open_threads")
        self.assertEqual(STATE.calls[0]["arguments"], {"limit": 3})
        self.assertIn("thread_20260817_190115_5b4eadd6", text)
        self.assertIn("returned 1 of 170 matched", text)

    def test_heartbeat_needs_no_call_endpoint(self):
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_heartbeat"))
        self.assertIn("status: ok", text)
        self.assertIn("1.15.0-fake", text)
        # 52, not 97: the fixture tracks the stack's published surface after the
        # 2026-09-06 retirement of 48 uncalled tools. It is a FIXTURE, not a
        # measurement — the live count is whatever /api/heartbeat says.
        self.assertIn("tools: 52", text)
        self.assertEqual(STATE.calls, [], "heartbeat must not touch POST /api/call")


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation(ShimTestCase):
    def test_truncation_marker_fires_and_states_both_numbers(self):
        big = "X" * 5000
        STATE.result_payload = default_recall_result(items=[{
            "timestamp": "2026-08-24T01:00:00+00:00",
            "domain": "big",
            "content": big,
            "layer": "hypothesis",
        }])
        proc = self.spawn(max_chars=500)
        proc.handshake()
        text = self.text_of(proc.request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "stack_recall", "arguments": {"query": "big"}},
        }))
        self.assertIn("[truncated, 500 of ", text)
        self.assertIn(" chars]", text)
        self.assertTrue(text.startswith("Sovereign Stack recall"))
        # 500 kept + the marker itself, which must never be the part cut off.
        self.assertTrue(text.rstrip().endswith("chars]"))
        self.assertLess(len(text), 700)

    def test_no_marker_when_under_the_cap(self):
        proc = self.spawn(max_chars=100000)
        proc.handshake()
        text = self.text_of(proc.request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "stack_recall", "arguments": {"query": "small"}},
        }))
        self.assertNotIn("[truncated", text)

    def test_truncate_unit_behaviour(self):
        self.assertEqual(shim.truncate("abc", 10), "abc")
        self.assertEqual(shim.truncate("abcdef", 3), "abc\n\n[truncated, 3 of 6 chars]")
        self.assertEqual(shim.truncate("abcdef", 6), "abcdef")


# ---------------------------------------------------------------------------
# The read-only boundary
# ---------------------------------------------------------------------------


class TestReadOnlyBoundary(unittest.TestCase):
    """These test the internal functions directly — no server, no network."""

    def test_allowlist_is_exactly_the_five_read_tools(self):
        # stack_latest reuses recall_insights: seven doors, five POST targets and
        # one GET path. This constant IS the boundary — it is pinned by name so a
        # widening is a diff a reviewer sees, and every name here is a READ.
        self.assertEqual(
            shim.ALLOWED_BRIDGE_TOOLS,
            frozenset({
                "recall_insights", "get_open_threads",
                "arrive_lineage", "current_policies", "signals_summary",
            }),
        )
        self.assertEqual(shim.ALLOWED_BRIDGE_PATHS, frozenset({"/api/heartbeat"}))
        self.assertEqual(len(shim.BRIDGE_TARGETS), 7)

    def test_every_allowlisted_tool_is_classified_by_result_type(self):
        """An unclassified addition is a red suite, not an unguarded door.

        The type guard is bidirectional (a string where an object is expected is
        the bridge fail-open; an object where text is expected is a changed tool
        shape), so a name in neither set would be reachable by neither door — and
        a name in BOTH would be checked by whichever helper ran first.
        """
        self.assertEqual(shim.TEXT_RESULT_TOOLS | shim.JSON_RESULT_TOOLS, shim.ALLOWED_BRIDGE_TOOLS)
        self.assertEqual(shim.TEXT_RESULT_TOOLS & shim.JSON_RESULT_TOOLS, frozenset())

    def test_json_and_text_doors_refuse_each_other(self):
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim.bridge_call("current_policies", {}, transport=grant_transport("http://127.0.0.1:1"))
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim.bridge_call_text("recall_insights", {}, transport=grant_transport("http://127.0.0.1:1"))

    def test_bridge_call_refuses_non_allowlisted_tools(self):
        for forbidden in (
            "record_insight",
            "handoff",
            "close_session",
            "record_open_thread",
            "spiral_inherit",
            "resolve_thread_by_id",
            "where_did_i_leave_off",
            "",
        ):
            with self.subTest(tool=forbidden):
                with self.assertRaises(shim.BridgeToolNotAllowed):
                    shim.bridge_call(forbidden, {}, transport=grant_transport("http://127.0.0.1:1"))
                with self.assertRaises(shim.BridgeToolNotAllowed):
                    shim.bridge_call_text(forbidden, {}, transport=grant_transport("http://127.0.0.1:1"))

    def test_refusal_happens_before_any_network_work(self):
        # base_url points at a port nothing listens on, and the socket path does
        # not exist. A BridgeError would mean the refusal came too late;
        # BridgeToolNotAllowed means it came first, on BOTH transports.
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim.bridge_call("record_insight", {"content": "x"}, transport=grant_transport("http://127.0.0.1:1"))
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim._post_envelope("record_insight", {}, transport=seat_transport("/nonexistent/bridge.sock"))

    def test_exposed_tools_map_exactly_onto_the_allowlisted_targets(self):
        """No exposed tool may reach a target that is not allowlisted, and no
        allowlisted target may sit there unreachable/unaccounted for."""
        self.assertEqual({tool["name"] for tool in shim.TOOL_DEFINITIONS}, set(shim.BRIDGE_TARGETS))
        post_targets = {target for method, target in shim.BRIDGE_TARGETS.values() if method == "POST"}
        get_targets = {target for method, target in shim.BRIDGE_TARGETS.values() if method == "GET"}
        self.assertEqual(post_targets, set(shim.ALLOWED_BRIDGE_TOOLS))
        self.assertEqual(get_targets, set(shim.ALLOWED_BRIDGE_PATHS))

    def test_no_known_write_tool_is_reachable_by_any_route(self):
        write_tools = {
            "record_insight", "record_open_thread", "handoff", "close_session",
            "spiral_inherit", "reflection_ack", "resolve_thread_by_id",
            "record_method", "promote_method",
        }
        self.assertEqual(shim.ALLOWED_BRIDGE_TOOLS & write_tools, frozenset())
        for name in sorted(write_tools):
            with self.subTest(tool=name):
                with self.assertRaises(shim.BridgeToolNotAllowed):
                    shim.call_tool(name, {})

    def test_call_tool_refuses_an_unknown_mcp_tool_name(self):
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim.call_tool("record_insight", {"content": "x"})


class TestBoundaryOverTheWire(ShimTestCase):
    def test_tools_call_with_a_bridge_tool_name_is_refused(self):
        proc = self.spawn()
        proc.handshake()
        response = proc.request({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "record_insight", "arguments": {"content": "should never land"}},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("refused", self.text_of(response).lower())
        self.assertEqual(STATE.calls, [], "a refused tool must not reach the bridge")


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


class TestFailsClosed(ShimTestCase):
    def test_bridge_unreachable_is_a_legible_error_not_an_empty_success(self):
        proc = ShimProcess("http://127.0.0.1:1")  # nothing listens here
        self.shims.append(proc)
        proc.handshake()
        response = proc.request({
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "stack_recall", "arguments": {"query": "anything"}},
        })
        self.assertTrue(response["result"]["isError"])
        text = self.text_of(response)
        self.assertIn("unavailable", text.lower())
        self.assertIn("failed call, not an empty result", text)

    def test_unknown_tool_string_result_under_http_200_is_treated_as_an_error(self):
        # The real bridge answers HTTP 200 {"ok":true,"result":"Unknown tool: x"}.
        # A success-shaped failure must not be rendered as content.
        STATE.raw_response = json.dumps({"ok": True, "result": "Unknown tool: recall_insights", "duration_ms": 1})
        with self.assertRaises(shim.BridgeError) as caught:
            shim.bridge_call("recall_insights", {"query": "x"}, transport=grant_transport(self.base_url))
        self.assertIn("Unknown tool", str(caught.exception))

    def test_text_door_catches_the_same_fail_open_shape(self):
        # The object-result guard cannot serve a text door, so the text door
        # carries its own belt for a stack old enough to RETURN "Unknown tool"
        # instead of raising. (On the deployed stack the envelope ok check gets
        # there first; this is the older-stack case, stated as such.)
        STATE.raw_response = json.dumps({"ok": True, "result": "Unknown tool: current_policies"})
        with self.assertRaises(shim.BridgeError) as caught:
            shim.bridge_call_text("current_policies", {}, transport=grant_transport(self.base_url))
        self.assertIn("Unknown tool", str(caught.exception))

    def test_text_door_refuses_an_object_result(self):
        # The other half of the bidirectional pin: a tool that starts returning
        # JSON must not be stringified into plausible prose.
        STATE.payload_by_tool = {"current_policies": {"policies": []}}
        with self.assertRaises(shim.BridgeError) as caught:
            shim.bridge_call_text("current_policies", {}, transport=grant_transport(self.base_url))
        self.assertIn("output shape changed", str(caught.exception))

    def test_text_door_refuses_empty_text(self):
        STATE.payload_by_tool = {"current_policies": "   "}
        with self.assertRaises(shim.BridgeError):
            shim.bridge_call_text("current_policies", {}, transport=grant_transport(self.base_url))

    def test_http_401_is_an_error_carrying_the_bridge_detail(self):
        with self.assertRaises(shim.BridgeError) as caught:
            shim.bridge_call("recall_insights", {"query": "x"},
                             transport=grant_transport(self.base_url, REJECTED_CREDENTIAL))
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertIn("failure_class", message)  # the bridge's own detail is relayed
        self.assertNotIn(REJECTED_CREDENTIAL, message)  # never echo a credential

    def test_not_ok_envelope_is_an_error(self):
        STATE.raw_response = json.dumps({"ok": False, "error": "boom"})
        with self.assertRaises(shim.BridgeError):
            shim.bridge_call("recall_insights", {"query": "x"}, transport=grant_transport(self.base_url))
        # The same envelope check guards the text doors — one gate, both types.
        with self.assertRaises(shim.BridgeError):
            shim.bridge_call_text("current_policies", {}, transport=grant_transport(self.base_url))

    def test_signal_ledger_blind_is_an_error_not_an_empty_queue(self):
        # signals_summary answers ok:FALSE inside an ok:true envelope when the
        # ledger cannot be read at all. That is a refusal, and rendering it as
        # "0 unacked" would be this house's own fail-open reproduced.
        STATE.payload_by_tool = {
            "signals_summary": {"ok": False, "error": "signal_ledger_unavailable", "ingestion": "not_scanned"}
        }
        proc = self.spawn()
        proc.handshake()
        response = proc.request({
            "jsonrpc": "2.0", "id": 21, "method": "tools/call",
            "params": {"name": "stack_signals", "arguments": {}},
        })
        self.assertTrue(response["result"]["isError"])
        text = self.text_of(response)
        self.assertIn("signal_ledger_unavailable", text)
        self.assertNotIn("unacked total 0", text)


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


class TestTransportResolution(unittest.TestCase):
    """The credential path: two transports, chosen by configuration, never guessed.

    Anthony's rule of 2026-09-05 forbids the master key as a seat credential
    everywhere. Until 0.4.0 this shim read it out of a shell-sourceable env file
    by default, which is exactly the thing the rule forbids, so that path is
    gone and its variable is refused by name.
    """

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in TRANSPORT_ENV_VARS}
        for name in TRANSPORT_ENV_VARS:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_importing_the_module_does_not_exit_without_a_transport(self):
        # Resolution is lazy, inside main()/resolve_transport(), precisely so
        # importing the module for testing never touches it.
        self.assertTrue(callable(shim.resolve_transport))

    def test_the_env_file_variable_is_refused_by_name(self):
        os.environ[shim.ENV_FILE_VAR] = "/tmp/anything.env"
        os.environ[shim.TOKEN_ENV_VAR] = DUMMY_CREDENTIAL
        with self.assertRaises(shim.TransportRefused) as caught:
            shim.resolve_transport()
        message = str(caught.exception)
        self.assertIn(shim.ENV_FILE_VAR, message)
        self.assertIn("never carries the master key", message)
        # The refusal names both real doors, so the redirect is actionable.
        self.assertIn(shim.SEAT_ENV_VAR, message)
        self.assertIn(shim.TOKEN_ENV_VAR, message)

    def test_the_env_file_reader_is_gone_entirely(self):
        # Not merely bypassed: the parser and its constants are deleted, so no
        # future edit can reconnect the master key by flipping one branch.
        for attribute in ("parse_env_file", "load_token", "DEFAULT_ENV_FILE", "TOKEN_ENV_KEY"):
            self.assertFalse(hasattr(shim, attribute), f"{attribute} still exists")

    def test_seat_transport_resolves_from_the_seat_variable_alone(self):
        os.environ[shim.SEAT_ENV_VAR] = DUMMY_SEAT
        transport = shim.resolve_transport()
        self.assertEqual(transport.kind, "seat")
        self.assertEqual(transport.seat, DUMMY_SEAT)
        self.assertEqual(transport.socket_path, os.path.expanduser(shim.DEFAULT_SEAT_SOCKET))
        self.assertIsNone(transport.token)

    def test_socket_override_wins_over_the_default_path(self):
        os.environ[shim.SEAT_ENV_VAR] = DUMMY_SEAT
        os.environ[shim.SOCKET_ENV_VAR] = "/tmp/elsewhere.sock"
        self.assertEqual(shim.resolve_transport().socket_path, "/tmp/elsewhere.sock")

    def test_socket_without_a_seat_id_is_refused(self):
        os.environ[shim.SOCKET_ENV_VAR] = "/tmp/elsewhere.sock"
        with self.assertRaises(shim.TransportRefused) as caught:
            shim.resolve_transport()
        self.assertIn(shim.SEAT_ENV_VAR, str(caught.exception))

    def test_grant_transport_resolves_from_the_token_variable(self):
        os.environ[shim.TOKEN_ENV_VAR] = DUMMY_CREDENTIAL
        transport = shim.resolve_transport()
        self.assertEqual(transport.kind, "grant")
        self.assertIsNone(transport.seat)

    def test_both_transports_configured_is_refused_not_ordered(self):
        # "Chosen by configuration, never by guessing" means an ambiguous
        # configuration has no winner. A precedence rule here would be a guess
        # wearing a policy costume.
        os.environ[shim.SEAT_ENV_VAR] = DUMMY_SEAT
        os.environ[shim.TOKEN_ENV_VAR] = DUMMY_CREDENTIAL
        with self.assertRaises(shim.TransportRefused) as caught:
            shim.resolve_transport()
        message = str(caught.exception)
        self.assertIn("two transports", message)
        self.assertIn("never by guessing", message)

    def test_no_transport_at_all_is_refused_and_names_both_doors(self):
        with self.assertRaises(shim.TransportRefused) as caught:
            shim.resolve_transport()
        message = str(caught.exception)
        self.assertIn(shim.SEAT_ENV_VAR, message)
        self.assertIn(shim.TOKEN_ENV_VAR, message)

    def test_resolution_is_not_cached(self):
        os.environ[shim.TOKEN_ENV_VAR] = DUMMY_CREDENTIAL
        self.assertEqual(shim.resolve_transport().kind, "grant")
        os.environ.pop(shim.TOKEN_ENV_VAR)
        os.environ[shim.SEAT_ENV_VAR] = DUMMY_SEAT
        self.assertEqual(shim.resolve_transport().kind, "seat")

    def test_no_transport_at_startup_exits_one_with_a_single_stderr_line(self):
        proc = subprocess.run(
            [sys.executable, SHIM_PATH], input="", capture_output=True, text=True,
            env=clean_env(), timeout=30,
        )
        self.assertEqual(proc.returncode, 1)
        stderr = proc.stderr.strip()
        self.assertEqual(len(stderr.splitlines()), 1, f"expected one stderr line, got: {stderr!r}")
        self.assertIn("no transport is configured", stderr)

    def test_the_env_file_variable_also_stops_startup(self):
        proc = subprocess.run(
            [sys.executable, SHIM_PATH], input="", capture_output=True, text=True,
            env=clean_env(**{shim.ENV_FILE_VAR: "/tmp/anything.env", shim.TOKEN_ENV_VAR: DUMMY_CREDENTIAL}),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn(shim.ENV_FILE_VAR, proc.stderr)
        self.assertNotIn(DUMMY_CREDENTIAL, proc.stderr)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    def test_limit_clamping(self):
        self.assertEqual(shim._clamp_limit(None), 5)
        self.assertEqual(shim._clamp_limit(0), 1)
        self.assertEqual(shim._clamp_limit(-4), 1)
        self.assertEqual(shim._clamp_limit(7), 7)
        self.assertEqual(shim._clamp_limit(11), 10)
        self.assertEqual(shim._clamp_limit("3"), 3)
        self.assertEqual(shim._clamp_limit("banana"), 5)
        self.assertEqual(shim._clamp_limit(True), 5)

    def test_protocol_version_negotiation(self):
        self.assertEqual(shim.negotiate_protocol_version("2025-03-26"), "2025-03-26")
        self.assertEqual(shim.negotiate_protocol_version("nope"), shim.PREFERRED_PROTOCOL_VERSION)
        self.assertEqual(shim.negotiate_protocol_version(None), shim.PREFERRED_PROTOCOL_VERSION)
        self.assertEqual(shim.negotiate_protocol_version(2025), shim.PREFERRED_PROTOCOL_VERSION)

    def test_coverage_line_states_partiality(self):
        line = shim._coverage_line(default_recall_result())
        self.assertIn("returned 1 of 786 matched", line)
        self.assertIn("bridge-side truncated", line)
        self.assertIn("offset 1", line)

    def test_coverage_line_survives_a_bare_result(self):
        self.assertIn("returned 0", shim._coverage_line({}))


class TestRedirects(unittest.TestCase):
    """A 302 must not copy Authorization onto the next hop (P0, live-probed)."""

    def test_http_json_refuses_redirect_and_does_not_forward_authorization(self):
        leaked = []

        class Sink(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                leaked.append(self.headers.get("Authorization"))
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Bounce(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            sink_url = ""

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", self.sink_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        bounce = ThreadingHTTPServer(("127.0.0.1", 0), Bounce)
        Bounce.sink_url = f"http://127.0.0.1:{sink.server_address[1]}/sink"
        threading.Thread(target=sink.serve_forever, daemon=True).start()
        threading.Thread(target=bounce.serve_forever, daemon=True).start()
        probe = "redirect-probe-value-123456"
        try:
            with self.assertRaises(shim.BridgeError) as ctx:
                shim._http_json(
                    "GET",
                    "/start",
                    transport=grant_transport(
                        f"http://127.0.0.1:{bounce.server_address[1]}", probe
                    ),
                    timeout=2,
                )
            self.assertIn("redirect", str(ctx.exception).lower())
            self.assertEqual(leaked, [])
        finally:
            bounce.shutdown()
            sink.shutdown()
            bounce.server_close()
            sink.server_close()



# ---------------------------------------------------------------------------
# The SEAT SOCKET transport
# ---------------------------------------------------------------------------


class SeatSocketTestCase(unittest.TestCase):
    """One fake seat socket for the class; one shim process per test."""

    @classmethod
    def setUpClass(cls):
        cls.bridge = SeatSocketBridge()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.close()

    def setUp(self):
        STATE.reset()
        self.shims = []

    def tearDown(self):
        for proc in self.shims:
            proc.close()

    def spawn(self, **kwargs):
        kwargs.setdefault("socket_path", self.bridge.path)
        proc = ShimProcess(**kwargs)
        self.shims.append(proc)
        return proc

    @staticmethod
    def text_of(response):
        content = response["result"]["content"]
        return "".join(part["text"] for part in content if part.get("type") == "text")


class TestSeatSocketTransport(SeatSocketTestCase):
    def test_a_call_over_the_socket_sends_the_seat_header_and_no_authorization(self):
        """The whole point of the seat transport: identity, no credential.

        The bridge decides on the ORDER — an Authorization header of any kind
        routes to the bearer check and the seat path is never reached — so a
        shim that sent both would not be "belt and braces", it would silently
        stop being a seat.
        """
        proc = self.spawn()
        proc.handshake()
        response = proc.request({
            "jsonrpc": "2.0", "id": 30, "method": "tools/call",
            "params": {"name": "stack_recall", "arguments": {"query": "temple-harness"}},
        })
        self.assertFalse(response["result"]["isError"], self.text_of(response))
        self.assertEqual(STATE.header_of("X-Sovereign-Seat"), DUMMY_SEAT)
        self.assertIsNone(STATE.header_of("Authorization"))
        self.assertEqual(STATE.calls[0]["tool"], "recall_insights")

    def test_the_heartbeat_also_rides_the_socket(self):
        proc = self.spawn()
        proc.handshake()
        response = proc.request({
            "jsonrpc": "2.0", "id": 31, "method": "tools/call",
            "params": {"name": "stack_heartbeat", "arguments": {}},
        })
        text = self.text_of(response)
        self.assertIn("seat socket", text)
        self.assertIn(DUMMY_SEAT, text)
        self.assertIsNone(STATE.header_of("Authorization"))

    def test_a_wrong_seat_id_is_refused_by_the_bridge_and_fails_closed(self):
        proc = self.spawn(seat="not-a-registered-seat")
        proc.handshake()
        response = proc.request({
            "jsonrpc": "2.0", "id": 32, "method": "tools/call",
            "params": {"name": "stack_recall", "arguments": {"query": "x"}},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("401", self.text_of(response))

    def test_an_absent_socket_is_a_legible_error_not_an_empty_result(self):
        proc = self.spawn(socket_path=os.path.join(self.bridge.directory, "no-such.sock"))
        proc.handshake()
        response = proc.request({
            "jsonrpc": "2.0", "id": 33, "method": "tools/call",
            "params": {"name": "stack_recall", "arguments": {"query": "x"}},
        })
        self.assertTrue(response["result"]["isError"])
        text = self.text_of(response)
        self.assertIn("seat socket not found", text)
        self.assertIn("failed call, not an empty result", text)

    def test_a_bound_but_unlistening_socket_says_nothing_is_accepting(self):
        """The live failure of 2026-09-06, kept as a test.

        A socket file that exists while nothing accepts on it is a SERVER fact.
        Reported as a generic transport error it reads as a client fault, and an
        operator spends the evening on the wrong side of the connection.
        """
        directory = tempfile.mkdtemp(prefix="temple-seat-bound-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "bridge.sock")
        bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound.bind(path)  # bind WITHOUT listen: the exact live shape
        self.addCleanup(bound.close)
        with self.assertRaises(shim.BridgeError) as caught:
            shim.bridge_call("recall_insights", {"query": "x"},
                             transport=seat_transport(path), timeout=5)
        message = str(caught.exception)
        self.assertIn("nothing is", message)
        self.assertIn("check the bridge, not this shim", message)

    def test_a_redirect_on_the_socket_is_refused_too(self):
        """Law 2 on the transport this shim gained, not only the one it had.

        There is no Authorization header to steal here, which is exactly why the
        refusal has to be justified on its own terms: following a 3xx would mean
        trusting a hop nobody verified, and the bridge has no legitimate redirect
        on either transport.
        """
        STATE.raw_response = None
        directory = tempfile.mkdtemp(prefix="temple-seat-redirect-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "bridge.sock")

        class Bounce(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://example.invalid/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = _UnixHTTPServer(path, Bounce)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(shim.BridgeError) as caught:
                shim.bridge_call(
                    "recall_insights", {"query": "x"},
                    transport=seat_transport(path), timeout=5,
                )
            self.assertIn("redirect", str(caught.exception).lower())
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# The three new doors
# ---------------------------------------------------------------------------


class TestNewDoors(ShimTestCase):
    def call(self, proc, name, arguments=None, request_id=40):
        return proc.request({
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    # -- stack_arrive --------------------------------------------------------

    def test_arrive_forwards_the_reader_full_content_and_a_clamped_bucket(self):
        STATE.payload_by_tool = {"arrive_lineage": FAKE_ARRIVAL_TEXT}
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_arrive", {"limit_per_bucket": 500})
        self.assertFalse(response["result"]["isError"], self.text_of(response))
        forwarded = STATE.calls[0]
        self.assertEqual(forwarded["tool"], "arrive_lineage")
        self.assertEqual(forwarded["arguments"]["source_instance"], DUMMY_SEAT_NAME)
        self.assertIs(forwarded["arguments"]["full_content"], True)
        self.assertEqual(forwarded["arguments"]["limit_per_bucket"], shim.BUCKET_MAX)

    def test_arrive_renders_the_doors_own_text_under_a_coverage_line(self):
        STATE.payload_by_tool = {"arrive_lineage": FAKE_ARRIVAL_TEXT}
        proc = self.spawn(max_chars=8000)
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_arrive"))
        self.assertIn("arrival coverage:", text)
        self.assertIn(DUMMY_SEAT_NAME, text)
        self.assertIn("does not re-count", text)
        self.assertIn("ARRIVE_LINEAGE", text)

    def test_arrive_refuses_when_the_reader_name_is_unset(self):
        proc = self.spawn(seat_name=None)
        proc.handshake()
        response = self.call(proc, "stack_arrive")
        self.assertTrue(response["result"]["isError"])
        self.assertIn("TEMPLE_SEAT_NAME", self.text_of(response))
        self.assertEqual(STATE.calls, [], "a refused door must not reach the bridge")

    def test_arrive_refuses_a_decorated_reader_name(self):
        # The documented trap: the to_self addressee filter matches the BARE
        # model name only, so a decorated seat string hides that line's letters.
        proc = self.spawn(seat_name="HQ Mac Studio - claude-fable-5")
        proc.handshake()
        response = self.call(proc, "stack_arrive")
        self.assertTrue(response["result"]["isError"])
        self.assertIn("decorated", self.text_of(response))
        self.assertEqual(STATE.calls, [])

    # -- stack_policies ------------------------------------------------------

    def test_policies_sends_no_filter_and_states_its_coverage(self):
        STATE.payload_by_tool = {"current_policies": FAKE_POLICIES_TEXT}
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_policies"))
        self.assertEqual(STATE.calls[0], {"tool": "current_policies", "arguments": {}})
        self.assertIn("policies coverage:", text)
        self.assertIn("Standing policies", text)

    # -- stack_signals -------------------------------------------------------

    def test_signals_pins_summary_mode_outside_the_schema(self):
        STATE.payload_by_tool = {"signals_summary": default_signals_summary()}
        proc = self.spawn()
        proc.handshake()
        self.call(proc, "stack_signals")
        self.assertEqual(STATE.calls[0]["arguments"], {"mode": "summary"})

    def test_signals_renders_every_field_the_contract_names(self):
        STATE.payload_by_tool = {"signals_summary": default_signals_summary()}
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_signals"))
        self.assertIn("signals coverage:", text)
        self.assertIn("unacked total 3", text)
        self.assertIn("stale_24h 1", text)
        self.assertIn("stale_7d 0", text)
        self.assertIn("ingestion: fresh", text)
        self.assertIn("honk:", text)
        self.assertIn("guardian:", text)

    def test_signals_never_renders_a_zero_the_envelope_did_not_measure(self):
        """A null count is 'unmeasured'. Printing 0 would be the house fail-open.

        The guardian source in the fixture is unavailable, so every one of its
        counts is null; the aggregate `total` is null too when the ledger could
        not answer. Neither may read as a healthy zero.
        """
        STATE.payload_by_tool = {
            "signals_summary": default_signals_summary(total=None, stale_24h=None, stale_7d=None)
        }
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_signals"))
        self.assertIn("unacked total unmeasured", text)
        self.assertIn("stale_24h unmeasured", text)
        self.assertNotIn("unacked total 0", text)
        self.assertIn("guardian: oldest_unacked unmeasured", text)

    def test_signals_partial_read_shows_counts_and_an_error_line(self):
        # ok:true WITH an error is a partial read: real rows, something else
        # unmeasured. It is neither all-clear nor a failed call.
        STATE.payload_by_tool = {
            "signals_summary": default_signals_summary(error="guardian probe unavailable")
        }
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_signals")
        self.assertFalse(response["result"]["isError"])
        text = self.text_of(response)
        self.assertIn("error: guardian probe unavailable", text)
        self.assertIn("PARTIAL read, not all-clear", text)
        self.assertIn("unacked total 3", text)

    # -- heartbeat -----------------------------------------------------------

    def test_heartbeat_carries_the_unacked_signal_total(self):
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_heartbeat"))
        self.assertIn("unacked signals: 3", text)

    def test_heartbeat_unmeasured_signals_do_not_read_as_zero(self):
        STATE.heartbeat = dict(
            STATE.heartbeat,
            unacked_signals={"total": None, "error": "signal_ledger_unavailable"},
        )
        self.addCleanup(STATE.heartbeat.pop, "unacked_signals", None)
        proc = self.spawn()
        proc.handshake()
        text = self.text_of(self.call(proc, "stack_heartbeat"))
        self.assertIn("unacked signals: unmeasured", text)
        self.assertIn("signal_ledger_unavailable", text)


class TestNewDoorTruncation(ShimTestCase):
    def test_arrival_truncation_states_both_numbers_and_keeps_the_coverage_line(self):
        """Two truncations, kept distinguishable — and coverage survives the cut.

        The coverage line is rendered FIRST for exactly this reason: a character
        cap that ate the coverage statement would leave a partial result looking
        whole, which is the failure the coverage line exists to prevent.
        """
        STATE.payload_by_tool = {"arrive_lineage": "L" * 5000}
        proc = self.spawn(max_chars=400)
        proc.handshake()
        text = self.text_of(proc.request({
            "jsonrpc": "2.0", "id": 45, "method": "tools/call",
            "params": {"name": "stack_arrive", "arguments": {}},
        }))
        self.assertIn("arrival coverage:", text)
        self.assertIn("[truncated, 400 of ", text)

    def test_policies_truncation_states_both_numbers(self):
        STATE.payload_by_tool = {"current_policies": "P" * 5000}
        proc = self.spawn(max_chars=300)
        proc.handshake()
        text = self.text_of(proc.request({
            "jsonrpc": "2.0", "id": 46, "method": "tools/call",
            "params": {"name": "stack_policies", "arguments": {}},
        }))
        self.assertIn("policies coverage:", text)
        self.assertIn("[truncated, 300 of ", text)


class TestArrivalOverrideIsStated(unittest.TestCase):
    """The seat transport OVERRIDES source_instance, and that is coverage.

    seat_identity.sign_arguments stamps the kernel-verified seat id over
    whatever the body said, for arrive_lineage among others. So on the Studio
    the door filters to_self letters for a REGISTRY SEAT ID, not for the bare
    model name this shim asked with. An empty to_self bucket is then a routing
    fact, and a reader not told that will read it as an absence.
    """

    def test_seat_transport_render_names_the_override(self):
        text = shim.render_arrival("body", "claude-fable-5", 5, seat_transport("/tmp/x.sock"))
        self.assertIn("OVERRODE source_instance", text)
        self.assertIn(DUMMY_SEAT, text)
        self.assertIn("not evidence of no mail", text)

    def test_grant_transport_render_does_not_claim_an_override(self):
        text = shim.render_arrival("body", "claude-fable-5", 5, grant_transport("http://x"))
        self.assertNotIn("OVERRODE", text)
        self.assertIn("arrival coverage:", text)


class TestDumpConfigTransport(unittest.TestCase):
    """--dump-config states WHICH transport resolved and WHY, never a value."""

    def _run(self, **overrides):
        return subprocess.run(
            [sys.executable, SHIM_PATH, "--dump-config"],
            capture_output=True, text=True, env=clean_env(**overrides), timeout=30,
        )

    def test_seat_transport_is_reported_with_its_reason(self):
        proc = self._run(SOVEREIGN_SEAT=DUMMY_SEAT)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("transport: seat", proc.stdout)
        self.assertIn("SOVEREIGN_SEAT is set", proc.stdout)
        self.assertIn("X-Sovereign-Seat: " + DUMMY_SEAT, proc.stdout)
        self.assertIn("authorization: none sent", proc.stdout)

    def test_grant_transport_is_reported_without_the_value(self):
        proc = self._run(TEMPLE_BRIDGE_TOKEN=SENTINEL_VALUE)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("transport: grant", proc.stdout)
        self.assertIn("TEMPLE_BRIDGE_TOKEN is set", proc.stdout)
        self.assertNotIn(SENTINEL_VALUE, proc.stdout)
        self.assertNotIn(SENTINEL_VALUE, proc.stderr)

    def test_ambiguity_is_reported_as_the_resolved_state_not_an_exit_code(self):
        # Inspection must not require a working configuration: dump-config is
        # how an operator SEES a broken one.
        proc = self._run(SOVEREIGN_SEAT=DUMMY_SEAT, TEMPLE_BRIDGE_TOKEN=SENTINEL_VALUE)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("transport: ABSENT", proc.stdout)
        self.assertIn("two transports", proc.stdout)
        self.assertNotIn(SENTINEL_VALUE, proc.stdout)

    def test_the_env_file_refusal_is_visible_in_the_dump(self):
        proc = self._run(TEMPLE_BRIDGE_ENV_FILE="/tmp/anything.env")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("transport: ABSENT", proc.stdout)
        self.assertIn("never carries the master key", proc.stdout)

    def test_the_result_type_split_is_printed(self):
        proc = self._run(SOVEREIGN_SEAT=DUMMY_SEAT)
        self.assertIn("object-result doors:", proc.stdout)
        self.assertIn("text-result doors:", proc.stdout)
        for name in sorted(shim.ALLOWED_BRIDGE_TOOLS):
            self.assertIn(name, proc.stdout)

    def test_the_reader_name_presence_is_reported(self):
        absent = self._run(SOVEREIGN_SEAT=DUMMY_SEAT)
        self.assertIn("TEMPLE_SEAT_NAME): ABSENT", absent.stdout)
        present = self._run(SOVEREIGN_SEAT=DUMMY_SEAT, TEMPLE_SEAT_NAME=DUMMY_SEAT_NAME)
        self.assertIn("TEMPLE_SEAT_NAME): " + DUMMY_SEAT_NAME, present.stdout)



if __name__ == "__main__":
    unittest.main(verbosity=2)
