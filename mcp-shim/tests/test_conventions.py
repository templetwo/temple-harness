#!/usr/bin/env python3
"""Enforcement tests for CONVENTIONS.md — the written laws, checked.

These tests exist so a convention break is a red suite, not a review nit.
They reuse the fake bridge and subprocess harness from test_shim.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from test_shim import (
    DUMMY_SEAT,
    DUMMY_SEAT_NAME,
    FAKE_ARRIVAL_TEXT,
    FAKE_POLICIES_TEXT,
    SHIM_PATH,
    STATE,
    ShimTestCase,
    clean_env,
    default_signals_summary,
    shim,
)

# Law 4: if a 9B cannot decide which door to take from the description alone,
# the description is wrong — and a description that long is not being decided
# from, it is being skimmed.
DESCRIPTION_CHAR_CAP = 700

SENTINEL_VALUE = "sentinel-value-that-must-never-print"

# What each door must forward, and the vocabulary its coverage line uses. This
# table is the fail-open fix: the old test iterated a HAND-WRITTEN list of three
# tools, so a new door needed no coverage line to keep the suite green. Driving
# it off TOOL_DEFINITIONS instead means an unlisted door is a red suite.
DOOR_FIXTURES = {
    "stack_recall": ({"query": "anything"}, "bridge coverage:"),
    "stack_latest": ({}, "bridge coverage:"),
    "stack_open_threads": ({}, "bridge coverage:"),
    "stack_arrive": ({}, "arrival coverage:"),
    "stack_policies": ({}, "policies coverage:"),
    "stack_signals": ({}, "signals coverage:"),
    "stack_heartbeat": ({}, "scope: READ-ONLY"),
}

# The payloads each bridge target answers with, so one fake serves every door.
DOOR_PAYLOADS = {
    "arrive_lineage": FAKE_ARRIVAL_TEXT,
    "current_policies": FAKE_POLICIES_TEXT,
    "signals_summary": default_signals_summary(),
}


class TestCoverageAlwaysStated(ShimTestCase):
    """Law 1: EVERY tool result states coverage (or scope, for heartbeat)."""

    def call(self, proc, name, arguments=None):
        return proc.request({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def test_every_door_is_covered_by_this_test(self):
        """The fail-open guard on the guard: no door may go unexercised here."""
        self.assertEqual(set(DOOR_FIXTURES), {t["name"] for t in shim.TOOL_DEFINITIONS})

    def test_every_door_states_coverage(self):
        for name, (arguments, marker) in sorted(DOOR_FIXTURES.items()):
            with self.subTest(tool=name):
                STATE.reset()
                STATE.payload_by_tool = dict(DOOR_PAYLOADS)
                proc = self.spawn(max_chars=8000)
                proc.handshake()
                response = self.call(proc, name, arguments)
                self.assertFalse(response["result"]["isError"], self.text_of(response))
                self.assertIn(marker, self.text_of(response))


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
        # The three doors added in 0.4.0 all answer questions a 9B could mistake
        # for a recall, so each one must say when NOT to take it.
        self.assertIn("stack_recall", by_name["stack_arrive"]["description"])
        self.assertIn("stack_latest", by_name["stack_arrive"]["description"])
        self.assertIn("stack_recall", by_name["stack_policies"]["description"])

    def test_every_schema_is_strict(self):
        for tool in shim.TOOL_DEFINITIONS:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertIs(schema.get("additionalProperties"), False)


class TestDumpConfig(unittest.TestCase):
    """Law 2 corollary: inspection needs no credentials and never leaks them."""

    def _run(self, **overrides):
        return subprocess.run(
            [sys.executable, SHIM_PATH, "--dump-config"],
            capture_output=True, text=True, env=clean_env(**overrides), timeout=30,
        )

    def test_runs_with_no_transport_at_all_and_says_absent(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ABSENT", proc.stdout)

    def test_grant_presence_reported_but_value_never_printed(self):
        proc = self._run(TEMPLE_BRIDGE_TOKEN=SENTINEL_VALUE)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("transport: grant", proc.stdout)
        self.assertNotIn(SENTINEL_VALUE, proc.stdout)
        self.assertNotIn(SENTINEL_VALUE, proc.stderr)

    def test_the_master_key_env_file_is_named_as_refused_never_read(self):
        """Replaces the 0.3.1 test that asserted the env-file token WORKED.

        That test proved the shim could load the master key out of a
        shell-sourceable file. Anthony's 2026-09-05 rule forbids exactly that,
        so the behaviour is gone and this is its inversion: the variable is
        named, the refusal is legible, and no value is involved at all.
        """
        proc = self._run(TEMPLE_BRIDGE_ENV_FILE="/tmp/definitely-absent.env")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("transport: ABSENT", proc.stdout)
        self.assertIn(shim.ENV_FILE_VAR, proc.stdout)
        self.assertIn("never carries the master key", proc.stdout)

    def test_all_doors_and_boundary_are_listed(self):
        proc = self._run(SOVEREIGN_SEAT=DUMMY_SEAT, TEMPLE_SEAT_NAME=DUMMY_SEAT_NAME)
        for name in shim.BRIDGE_TARGETS:
            self.assertIn(name, proc.stdout)
        for target in sorted(shim.ALLOWED_BRIDGE_TOOLS):
            self.assertIn(target, proc.stdout)
        self.assertIn("write lane: none", proc.stdout)

    def test_no_network_is_attempted(self):
        # bridge_url points at a port nothing listens on; dump must still succeed.
        proc = self._run(TEMPLE_BRIDGE_TOKEN=SENTINEL_VALUE, TEMPLE_BRIDGE_URL="http://127.0.0.1:1")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("http://127.0.0.1:1", proc.stdout)


class TestBoundaryIsADiff(unittest.TestCase):
    """Law 3: scope lives in module-level constants, and both halves are pinned."""

    def test_the_allowlist_widening_is_exactly_the_three_named_reads(self):
        """0.4.0 widens ALLOWED_BRIDGE_TOOLS by three POST names and no more.

        Named one by one rather than by count, because a count passes for the
        wrong set. This is the tier-two constant law 8 sends to Anthony.
        """
        self.assertEqual(
            shim.ALLOWED_BRIDGE_TOOLS - frozenset({"recall_insights", "get_open_threads"}),
            frozenset({"arrive_lineage", "current_policies", "signals_summary"}),
        )

    def test_no_write_tool_sits_in_either_result_type_set(self):
        writes = {"record_insight", "record_open_thread", "handoff", "close_session",
                  "spiral_inherit", "reflection_ack", "set_policy", "signal_ack"}
        self.assertEqual(shim.ALLOWED_BRIDGE_TOOLS & writes, frozenset())
        self.assertEqual((shim.TEXT_RESULT_TOOLS | shim.json_result_tools()) & writes, frozenset())


if __name__ == "__main__":
    unittest.main()
