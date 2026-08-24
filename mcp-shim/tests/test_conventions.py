#!/usr/bin/env python3
"""Enforcement tests for CONVENTIONS.md — the written laws, checked.

These tests exist so a convention break is a red suite, not a review nit.
They reuse the fake bridge and subprocess harness from test_shim.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from test_shim import STATE, SHIM_PATH, ShimTestCase, shim

# Law 4: if a 9B cannot decide which door to take from the description alone,
# the description is wrong — and a description that long is not being decided
# from, it is being skimmed.
DESCRIPTION_CHAR_CAP = 700

SENTINEL_TOKEN = "sentinel-token-that-must-never-print"


class TestCoverageAlwaysStated(ShimTestCase):
    """Law 1: every tool result states coverage (or scope, for heartbeat)."""

    def call(self, proc, name, arguments=None):
        return proc.request({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def test_every_post_tool_states_bridge_coverage(self):
        for name, arguments in (
            ("stack_recall", {"query": "anything"}),
            ("stack_latest", {}),
            ("stack_open_threads", {}),
        ):
            with self.subTest(tool=name):
                STATE.reset()
                proc = self.spawn()
                proc.handshake()
                response = self.call(proc, name, arguments)
                self.assertFalse(response["result"]["isError"])
                self.assertIn("bridge coverage:", self.text_of(response))

    def test_heartbeat_states_scope(self):
        proc = self.spawn()
        proc.handshake()
        response = self.call(proc, "stack_heartbeat")
        self.assertIn("scope: READ-ONLY", self.text_of(response))


class TestLegibleToA9B(unittest.TestCase):
    """Law 4: short routing-bearing descriptions, strict schemas."""

    def test_descriptions_exist_and_fit_the_cap(self):
        for tool in shim.TOOL_DEFINITIONS:
            with self.subTest(tool=tool["name"]):
                description = tool.get("description") or ""
                self.assertTrue(description.strip(), f"{tool['name']} has no description")
                self.assertLessEqual(
                    len(description), DESCRIPTION_CHAR_CAP,
                    f"{tool['name']} description is {len(description)} chars; "
                    f"law 4 caps it at {DESCRIPTION_CHAR_CAP}",
                )

    def test_overlapping_doors_carry_routing(self):
        by_name = {tool["name"]: tool for tool in shim.TOOL_DEFINITIONS}
        # The two doors that share a bridge target must route between themselves.
        self.assertIn("stack_recall", by_name["stack_latest"]["description"])
        self.assertIn("relevance", by_name["stack_recall"]["description"].lower())

    def test_every_schema_is_strict(self):
        for tool in shim.TOOL_DEFINITIONS:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertIs(schema.get("additionalProperties"), False)


class TestDumpConfig(unittest.TestCase):
    """Law 2 corollary: inspection needs no credentials and never leaks them."""

    def _run(self, extra_env):
        env = dict(os.environ)
        env.pop("TEMPLE_BRIDGE_TOKEN", None)
        env["TEMPLE_BRIDGE_ENV_FILE"] = os.path.join(
            tempfile.gettempdir(), "definitely-absent-env-file"
        )
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, SHIM_PATH, "--dump-config"],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_runs_without_any_token_and_says_absent(self):
        proc = self._run({})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ABSENT", proc.stdout)

    def test_token_presence_reported_but_value_never_printed(self):
        proc = self._run({"TEMPLE_BRIDGE_TOKEN": SENTINEL_TOKEN})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("present (TEMPLE_BRIDGE_TOKEN override)", proc.stdout)
        self.assertNotIn(SENTINEL_TOKEN, proc.stdout)
        self.assertNotIn(SENTINEL_TOKEN, proc.stderr)

    def test_env_file_token_branch_reported_and_never_printed(self):
        # The PRODUCTION token path: BRIDGE_TOKEN read from the env file.
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write(f"BRIDGE_TOKEN={SENTINEL_TOKEN}\n")
            env_file = handle.name
        self.addCleanup(os.unlink, env_file)
        env = dict(os.environ)
        env.pop("TEMPLE_BRIDGE_TOKEN", None)
        env["TEMPLE_BRIDGE_ENV_FILE"] = env_file
        proc = subprocess.run(
            [sys.executable, SHIM_PATH, "--dump-config"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f"present (BRIDGE_TOKEN in {env_file})", proc.stdout)
        self.assertNotIn(SENTINEL_TOKEN, proc.stdout)
        self.assertNotIn(SENTINEL_TOKEN, proc.stderr)

    def test_all_doors_and_boundary_are_listed(self):
        proc = self._run({})
        for name in shim.BRIDGE_TARGETS:
            self.assertIn(name, proc.stdout)
        for target in sorted(shim.ALLOWED_BRIDGE_TOOLS):
            self.assertIn(target, proc.stdout)
        self.assertIn("write lane: none", proc.stdout)

    def test_no_network_is_attempted(self):
        # bridge_url points at a port nothing listens on; dump must still succeed.
        proc = self._run({"TEMPLE_BRIDGE_URL": "http://127.0.0.1:1"})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("http://127.0.0.1:1", proc.stdout)


if __name__ == "__main__":
    unittest.main()
