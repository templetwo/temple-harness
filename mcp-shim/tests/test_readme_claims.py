#!/usr/bin/env python3
"""README/comment claims that can go stale. Counts are not pinned — they rot."""
from __future__ import annotations

import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SHIM_README = HERE.parent / "README.md"
ROOT_README = HERE.parent.parent / "README.md"
SHIM_PY = HERE.parent / "temple_stack_mcp.py"


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
