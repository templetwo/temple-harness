#!/usr/bin/env python3
"""Canary: mutate each shim gate, demand the probe leaks, restore.

Law 3: negative controls are review discipline. These tests ARE that
discipline, automated. Stdlib unittest only. No real bridge.

Run: python3 -m unittest tests.test_canary -v   (from mcp-shim/)
"""
from __future__ import annotations

import importlib.util
import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM_PATH = os.path.join(os.path.dirname(HERE), "temple_stack_mcp.py")


def load_shim():
    spec = importlib.util.spec_from_file_location("temple_stack_mcp_canary", SHIM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shim = load_shim()
DUMMY = "canary-not-a-real-token"


class ShimCanary(unittest.TestCase):
    def test_allowlist_is_the_write_refusal_gate(self):
        """If record_insight is added to the frozenset, refusal comes too late."""
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim.bridge_call(
                "record_insight", {"content": "x"},
                token=DUMMY, base_url="http://127.0.0.1:1",
            )
        saved = shim.ALLOWED_BRIDGE_TOOLS
        try:
            shim.ALLOWED_BRIDGE_TOOLS = saved | frozenset({"record_insight"})
            with self.assertRaises(shim.BridgeError):
                shim.bridge_call(
                    "record_insight", {"content": "x"},
                    token=DUMMY, base_url="http://127.0.0.1:1",
                )
        finally:
            shim.ALLOWED_BRIDGE_TOOLS = saved
        with self.assertRaises(shim.BridgeToolNotAllowed):
            shim.bridge_call(
                "record_insight", {"content": "x"},
                token=DUMMY, base_url="http://127.0.0.1:1",
            )

    def test_refuse_redirects_opener_is_the_token_gate(self):
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
        token = "canary-redirect-token-123456"
        url = f"http://127.0.0.1:{bounce.server_address[1]}/start"
        saved = shim._BRIDGE_OPENER
        try:
            with self.assertRaises(shim.BridgeError):
                shim._http_json("GET", url, token=token, timeout=2)
            self.assertEqual(leaked, [])

            leaked.clear()
            shim._BRIDGE_OPENER = urllib.request.build_opener()
            got = shim._http_json("GET", url, token=token, timeout=2)
            self.assertEqual(got, {"ok": True})
            self.assertEqual(leaked, [f"Bearer {token}"])
        finally:
            shim._BRIDGE_OPENER = saved
            bounce.shutdown()
            sink.shutdown()
            bounce.server_close()
            sink.server_close()

        leaked.clear()
        sink2 = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        bounce2 = ThreadingHTTPServer(("127.0.0.1", 0), Bounce)
        Bounce.sink_url = f"http://127.0.0.1:{sink2.server_address[1]}/sink"
        threading.Thread(target=sink2.serve_forever, daemon=True).start()
        threading.Thread(target=bounce2.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(shim.BridgeError):
                shim._http_json(
                    "GET",
                    f"http://127.0.0.1:{bounce2.server_address[1]}/start",
                    token=token,
                    timeout=2,
                )
            self.assertEqual(leaked, [])
        finally:
            bounce2.shutdown()
            sink2.shutdown()
            bounce2.server_close()
            sink2.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
