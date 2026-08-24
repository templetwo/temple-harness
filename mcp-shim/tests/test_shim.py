#!/usr/bin/env python3
"""Tests for the Temple Stack MCP shim.

Stdlib unittest only. These tests NEVER touch the real bridge and NEVER need the
real token: a fake bridge is stood up on a random localhost port and the shim is
pointed at it via TEMPLE_BRIDGE_URL, with TEMPLE_BRIDGE_TOKEN supplying a dummy
credential so the startup gate passes.

Run:  python3 -m unittest discover -s tests -v      (from mcp-shim/)
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM_PATH = os.path.join(os.path.dirname(HERE), "temple_stack_mcp.py")

# A dummy credential. The production path reads ~/.config/sovereign-bridge.env;
# nothing in this suite reads or needs the real value.
DUMMY_CREDENTIAL = "not-a-real-token"
# A credential the fake bridge deliberately rejects, so the HTTP 401 path is exercised.
REJECTED_CREDENTIAL = "rejected-by-the-fake-bridge"


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
        self.result_payload = None  # dict -> wrapped in {"ok":true,"result":...}
        self.raw_response = None    # str -> returned verbatim (fail-open shapes)
        self.heartbeat = {
            "status": "ok",
            "version": "1.15.0-fake",
            "tools": 97,
            "source_commit": "deadbee",
        }

    def reset(self):
        self.calls.clear()
        self.result_payload = None
        self.raw_response = None


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

    def do_GET(self):
        if self.path == "/api/heartbeat":
            self._send(200, json.dumps(STATE.heartbeat))
        else:
            self._send(404, json.dumps({"detail": "not found"}))

    def do_POST(self):
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

        authorization = self.headers.get("Authorization") or ""
        if not authorization.startswith("Bearer ") or authorization.endswith(REJECTED_CREDENTIAL):
            self._send(401, json.dumps({"detail": "Missing or malformed Bearer token.", "failure_class": "auth"}))
            return
        if STATE.raw_response is not None:
            self._send(200, STATE.raw_response)
            return
        payload = STATE.result_payload if STATE.result_payload is not None else default_recall_result()
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


# ---------------------------------------------------------------------------
# Subprocess harness
# ---------------------------------------------------------------------------


class ShimProcess:
    """Spawn the shim as a real subprocess and speak newline-delimited JSON-RPC."""

    def __init__(self, base_url, *, max_chars=None, token=DUMMY_CREDENTIAL, env_file=None):
        env = dict(os.environ)
        env.pop("TEMPLE_BRIDGE_TOKEN", None)
        env["TEMPLE_BRIDGE_URL"] = base_url
        env["TEMPLE_MCP_TIMEOUT"] = "10"
        if token is not None:
            env["TEMPLE_BRIDGE_TOKEN"] = token
        if max_chars is not None:
            env["TEMPLE_MCP_MAX_CHARS"] = str(max_chars)
        env["TEMPLE_BRIDGE_ENV_FILE"] = env_file or os.path.join(tempfile.gettempdir(), "definitely-absent-env-file")
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
        proc = ShimProcess(self.base_url, **kwargs)
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

    def test_exactly_three_tools(self):
        tools = self._tools()
        self.assertEqual(len(tools), 3)
        self.assertEqual(
            {tool["name"] for tool in tools},
            {"stack_recall", "stack_open_threads", "stack_heartbeat"},
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

        threads = by_name["stack_open_threads"]["inputSchema"]
        self.assertEqual(threads["required"], [])
        self.assertEqual(set(threads["properties"]), {"limit"})
        self.assertEqual(threads["properties"]["limit"]["maximum"], 10)

        heartbeat = by_name["stack_heartbeat"]["inputSchema"]
        self.assertEqual(heartbeat["properties"], {})
        self.assertFalse(heartbeat["additionalProperties"])

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
        self.assertIn("97", text)
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

    def test_allowlist_contains_only_the_two_read_tools(self):
        self.assertEqual(shim.ALLOWED_BRIDGE_TOOLS, frozenset({"recall_insights", "get_open_threads"}))
        self.assertEqual(shim.ALLOWED_BRIDGE_PATHS, frozenset({"/api/heartbeat"}))
        self.assertEqual(len(shim.BRIDGE_TARGETS), 3)

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
                    shim.bridge_call(forbidden, {}, token=DUMMY_CREDENTIAL, base_url="http://127.0.0.1:1")

    def test_refusal_happens_before_any_network_work(self):
        # base_url points at a port nothing listens on. A BridgeError would mean
        # the refusal came too late; BridgeToolNotAllowed means it came first.
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim.bridge_call("record_insight", {"content": "x"}, token=DUMMY_CREDENTIAL, base_url="http://127.0.0.1:1")

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
            shim.bridge_call("recall_insights", {"query": "x"}, token=DUMMY_CREDENTIAL, base_url=self.base_url)
        self.assertIn("Unknown tool", str(caught.exception))

    def test_http_401_is_an_error_carrying_the_bridge_detail(self):
        with self.assertRaises(shim.BridgeError) as caught:
            shim.bridge_call("recall_insights", {"query": "x"},
                             token=REJECTED_CREDENTIAL, base_url=self.base_url)
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertIn("failure_class", message)  # the bridge's own detail is relayed
        self.assertNotIn(REJECTED_CREDENTIAL, message)  # never echo a credential

    def test_not_ok_envelope_is_an_error(self):
        STATE.raw_response = json.dumps({"ok": False, "error": "boom"})
        with self.assertRaises(shim.BridgeError):
            shim.bridge_call("recall_insights", {"query": "x"}, token=DUMMY_CREDENTIAL, base_url=self.base_url)

    def test_no_token_at_startup_exits_one_with_a_single_stderr_line(self):
        proc = self.spawn(token=None, env_file=os.path.join(HERE, "does-not-exist.env"))
        proc.proc.wait(timeout=10)
        self.assertEqual(proc.proc.returncode, 1)
        stderr = proc.proc.stderr.read().strip()
        self.assertEqual(len(stderr.splitlines()), 1, f"expected one stderr line, got: {stderr!r}")
        self.assertIn("no bridge token", stderr)
        self.assertNotIn(DUMMY_CREDENTIAL, stderr)


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


class TestTokenHandling(unittest.TestCase):
    def test_importing_the_module_does_not_exit_without_a_token(self):
        # Already proven by this file importing at module scope, but assert the
        # contract explicitly so a future refactor cannot quietly break tests.
        self.assertTrue(callable(shim.load_token))

    def test_env_file_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bridge.env")
            lines = [
                "# a comment",
                "",
                f"{shim.TOKEN_ENV_KEY}={DUMMY_CREDENTIAL}",
                'OTHER_KEY="quoted value"',
                "export EXPORTED_KEY=exported-value",
                "MALFORMED_LINE_NO_EQUALS",
            ]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            parsed = shim.parse_env_file(path)
            self.assertEqual(parsed[shim.TOKEN_ENV_KEY], DUMMY_CREDENTIAL)
            self.assertEqual(parsed["OTHER_KEY"], "quoted value")
            self.assertEqual(parsed["EXPORTED_KEY"], "exported-value")
            self.assertNotIn("MALFORMED_LINE_NO_EQUALS", parsed)

    def test_missing_env_file_returns_empty_not_an_exception(self):
        self.assertEqual(shim.parse_env_file("/nonexistent/path/to/bridge.env"), {})

    def test_env_override_wins_over_the_file(self):
        previous = os.environ.get("TEMPLE_BRIDGE_TOKEN")
        os.environ["TEMPLE_BRIDGE_TOKEN"] = DUMMY_CREDENTIAL
        try:
            self.assertEqual(shim.load_token(), DUMMY_CREDENTIAL)
        finally:
            if previous is None:
                os.environ.pop("TEMPLE_BRIDGE_TOKEN", None)
            else:
                os.environ["TEMPLE_BRIDGE_TOKEN"] = previous


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
