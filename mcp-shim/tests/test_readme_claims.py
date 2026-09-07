#!/usr/bin/env python3
"""README/comment claims that can go stale. Counts are not pinned — they rot.

The four assertions below were written against the four-door shim (PR #6). The
three added under them generalise the same idea rather than restating it: instead
of forbidding one stale phrasing at a time, they hold the prose to the CODE, so a
door added tomorrow turns this file red until the docs name it. A count nobody
can check is the thing this file exists to stop; a NAME can be checked.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SHIM_README = HERE.parent / "README.md"
ROOT_README = HERE.parent.parent / "README.md"
SHIM_PY = HERE.parent / "temple_stack_mcp.py"


def load_shim():
    spec = importlib.util.spec_from_file_location("temple_stack_mcp_claims", SHIM_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shim = load_shim()


class TestReadmeClaims(unittest.TestCase):
    def test_shim_readme_does_not_say_exactly_three_tools(self):
        text = SHIM_README.read_text(encoding="utf-8")
        self.assertNotIn("exactly three tools", text.lower())
        self.assertIn("stack_latest", text)

    def test_shim_readme_does_not_claim_a_numeric_test_count(self):
        text = SHIM_README.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b\d+ tests\b", text))

    def test_root_readme_names_stack_latest_among_the_doors(self):
        text = ROOT_README.read_text(encoding="utf-8")
        self.assertIn("stack_latest", text)

    def test_boundary_comment_does_not_say_three_mcp_tools(self):
        text = SHIM_PY.read_text(encoding="utf-8")
        self.assertNotIn("Three MCP tools, three bridge targets", text)

    # ── derived from the code, so they cannot rot the way a count does ──────

    def test_shim_readme_names_every_door(self):
        text = SHIM_README.read_text(encoding="utf-8")
        for tool in shim.TOOL_DEFINITIONS:
            self.assertIn(tool["name"], text, f"{tool['name']} is undocumented")

    def test_root_readme_names_every_door(self):
        text = ROOT_README.read_text(encoding="utf-8")
        for tool in shim.TOOL_DEFINITIONS:
            self.assertIn(tool["name"], text, f"{tool['name']} is missing from the top-level README")

    def test_boundary_comment_names_every_allowlisted_post_target(self):
        """The boundary comment carries a count; the names are what make it checkable."""
        text = SHIM_PY.read_text(encoding="utf-8")
        head = text.split("BRIDGE_TARGETS = {", 1)[0]
        for target in sorted(shim.ALLOWED_BRIDGE_TOOLS):
            self.assertIn(target, head, f"{target} is allowlisted but unnamed in the boundary comment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
