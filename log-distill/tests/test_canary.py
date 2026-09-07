#!/usr/bin/env python3
"""Canary: mutate each distill gate, demand the probe leaks, restore.

Law 3: negative controls are review discipline. These tests ARE that
discipline, automated. If a gate is deleted, the corresponding canary
goes red because the broken-state probe no longer differs from the
healthy-state probe. Stdlib unittest only.

Run: python3 -m unittest tests.test_canary -v   (from log-distill/)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
DISTILL_PATH = os.path.join(os.path.dirname(HERE), "log_distill.py")


def load_module():
    spec = importlib.util.spec_from_file_location("log_distill_canary", DISTILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DistillCanary(unittest.TestCase):
    """Each test: healthy → masked; broken → leaks; restore → masked."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_redaction_patterns_are_the_gate(self):
        probe = "Bearer abcdefghijklmnopqrstu999SECRET"
        self.assertNotIn("SECRET", self.mod._redact(probe))
        original = self.mod._REDACT_PATTERNS[:]
        try:
            self.mod._REDACT_PATTERNS.clear()
            self.assertIn("SECRET", self.mod._redact(probe))
        finally:
            self.mod._REDACT_PATTERNS[:] = original
        self.assertNotIn("SECRET", self.mod._redact(probe))

    def test_list_emission_uses_redact(self):
        secret = "sk-" + "A" * 24
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = os.path.join(tmp, f"{secret}_session")
            os.makedirs(session_dir)
            with open(os.path.join(session_dir, "session.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "session", "id": "canary"}) + "\n")

            def run_list():
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = self.mod.main([tmp, "--list"])
                return code, out.getvalue()

            code, healthy = run_list()
            self.assertEqual(code, 0)
            self.assertNotIn(secret, healthy)
            self.assertIn(self.mod._REDACT_MARKER, healthy)

            original = self.mod._redact
            try:
                self.mod._redact = lambda text: text
                code, broken = run_list()
            finally:
                self.mod._redact = original
            self.assertEqual(code, 0)
            self.assertIn(secret, broken)

            code, restored = run_list()
            self.assertEqual(code, 0)
            self.assertNotIn(secret, restored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
