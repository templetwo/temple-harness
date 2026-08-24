#!/usr/bin/env python3
"""Tests for log-distill. Stdlib unittest only; fixtures are synthetic JSONL
sessions modeled on REAL dsh logs this house has diagnosed by hand:

  * the 2026-08-23 web-session cutoff (reasoning-only-stop),
  * the UNKNOWN_MODEL turn-error refusal,
  * an ordinary successful tool-using turn.

Run:  python3 -m unittest discover -s tests -v      (from log-distill/)
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
DISTILL_PATH = os.path.join(os.path.dirname(HERE), "log_distill.py")


def load_module():
    spec = importlib.util.spec_from_file_location("log_distill_under_test", DISTILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = load_module()


# ---------------------------------------------------------------------------
# Fixture builders — shapes copied from observed dsh events
# ---------------------------------------------------------------------------


def ev_session(session_id="session-test"):
    return {"type": "session", "version": 0, "id": session_id,
            "createdAt": 1787537867749, "cwd": "/tmp/workspace"}


def ev_turn_start(turn, time=1000):
    return {"type": "turn/start", "seq": 1, "time": time, "data": {"turn": turn}}


def ev_user(text, turn=None, kind="user"):
    """A user/message event. Real dsh events carry NO data.turn (verified
    across all real sessions — the parser's current-turn fallback is the path
    production always takes), and source.kind distinguishes the human ("user")
    from injected context ("plugin", "skill-catalog")."""
    data = {"content": [{"type": "text", "text": text}],
            "source": {"kind": kind}, "role": "user", "id": "u1"}
    if turn is not None:  # kept for constructing hypothetical shapes only
        data["turn"] = turn
    return {"type": "user/message", "seq": 2, "time": 1001, "data": data}


def ev_tool_call(turn, name="write", call_id="call_1", arguments='{"x":1}'):
    return {"type": "tool/call", "seq": 3, "time": 1002,
            "data": {"turn": turn, "step": 1, "callId": call_id,
                     "name": name, "arguments": arguments}}


def ev_tool_result(turn, call_id="call_1", text="ok", is_error=False):
    return {"type": "tool/result", "seq": 4, "time": 1003, "data": {
        "turn": turn, "step": 1, "message": {
            "source": {"kind": "tool", "callId": call_id},
            "content": [{"type": "tool-result", "toolCallId": call_id,
                         "content": [{"type": "text", "text": text}],
                         "isError": is_error}],
            "role": "user", "id": "t1"}}}


def ev_assistant(turn, text="", reasoning=""):
    content = []
    if reasoning:
        content.append({"type": "reasoning", "text": reasoning})
    if text:
        content.append({"type": "text", "text": text})
    return {"type": "assistant/message", "seq": 5, "time": 1004,
            "data": {"turn": turn, "step": 1,
                     "message": {"role": "assistant", "content": content}}}


def ev_usage(turn, input_tokens=100, output_tokens=50):
    return {"type": "assistant/chunk", "seq": 6, "time": 1005,
            "data": {"turn": turn, "step": 1, "chunk": {
                "type": "usage", "usage": {"inputTokens": input_tokens,
                                           "outputTokens": output_tokens}}}}


def ev_finish(turn, kind="stop"):
    return {"type": "assistant/chunk", "seq": 7, "time": 1006,
            "data": {"turn": turn, "step": 1,
                     "chunk": {"type": "finish", "reason": {"kind": kind}}}}


def ev_turn_end(turn, kind="completed", error=None, time=9000):
    reason = {"kind": kind}
    if error:
        reason["error"] = error
    return {"type": "turn/end", "seq": 8, "time": time,
            "data": {"turn": turn, "reason": reason}}


def good_turn(turn=1):
    return [
        ev_turn_start(turn), ev_user("do the thing"),
        ev_tool_call(turn), ev_tool_result(turn, text="file written"),
        ev_usage(turn), ev_finish(turn, "tool-calls"), ev_finish(turn, "stop"),
        ev_assistant(turn, text="Done: the file is written.", reasoning="brief think"),
        ev_turn_end(turn),
    ]


def cutoff_turn(turn=1):
    """The 2am shape: thought hard, emitted stop, no answer text."""
    return [
        ev_turn_start(turn), ev_user("recall 3 insights"),
        ev_usage(turn, 11159, 173), ev_finish(turn, "stop"),
        ev_assistant(turn, text="", reasoning="x" * 712),
        ev_turn_end(turn, "completed"),
    ]


def error_turn(turn=1):
    """The UNKNOWN_MODEL shape."""
    return [
        ev_turn_start(turn), ev_user("hello"),
        ev_turn_end(turn, "error", error={
            "message": 'pi-ai provider "ollama-local" has no configured model "x"',
            "code": "UNKNOWN_MODEL"}),
    ]


class DistillCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write_session(self, events, name="session.jsonl", extra_lines=None):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
            for line in extra_lines or []:
                handle.write(line + "\n")
        return path

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = distill.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestParsingAndRendering(DistillCase):
    def test_good_turn_renders_with_coverage(self):
        path = self.write_session([ev_session()] + good_turn())
        code, out, _err = self.run_main([path])
        self.assertEqual(code, 0)
        self.assertIn("turn 1 — end: completed", out)
        self.assertIn("tool: write", out)
        self.assertIn("Done: the file is written.", out)
        self.assertIn("anomalies: none detected", out)
        self.assertIn("coverage:", out)
        self.assertIn("all event types recognized", out)

    def test_unknown_event_types_are_counted_not_dropped_silently(self):
        events = [ev_session()] + good_turn() + [{"type": "future/event", "seq": 99}]
        path = self.write_session(events)
        code, out, _err = self.run_main([path])
        self.assertEqual(code, 0)
        self.assertIn("unrecognized event types: future/event×1", out)

    def test_unparseable_lines_are_counted(self):
        path = self.write_session([ev_session()] + good_turn(),
                                  extra_lines=["{not json", '"a bare string"'])
        code, out, _err = self.run_main([path])
        self.assertEqual(code, 0)
        self.assertIn("2 unparseable", out)

    def test_tail_limits_turns_and_coverage_says_so(self):
        events = [ev_session()] + good_turn(1) + good_turn(2)
        path = self.write_session(events)
        code, out, _err = self.run_main([path, "--tail", "1"])
        self.assertEqual(code, 0)
        self.assertNotIn("turn 1 —", out)
        self.assertIn("turn 2 —", out)
        self.assertIn("turns 1 of 2 shown", out)

    def test_long_text_is_clipped_and_says_so(self):
        events = [ev_session(), ev_turn_start(1), ev_user("y" * 500),
                  ev_assistant(1, text="z" * 500), ev_turn_end(1)]
        path = self.write_session(events)
        code, out, _err = self.run_main([path, "--max-text", "50"])
        self.assertEqual(code, 0)
        self.assertIn("[+450 chars]", out)


class TestAnomalies(DistillCase):
    def test_reasoning_only_stop_is_flagged(self):
        path = self.write_session([ev_session()] + cutoff_turn())
        code, out, _err = self.run_main([path])
        self.assertEqual(code, 0)
        self.assertIn("[reasoning-only-stop] turn 1", out)
        self.assertIn("712 chars", out)

    def test_turn_error_is_flagged_with_code(self):
        path = self.write_session([ev_session()] + error_turn())
        code, out, _err = self.run_main([path])
        self.assertEqual(code, 0)
        self.assertIn("[turn-error] turn 1", out)
        self.assertIn("UNKNOWN_MODEL", out)

    def test_tool_error_is_flagged(self):
        events = [ev_session(), ev_turn_start(1),
                  ev_tool_call(1), ev_tool_result(1, text="boom", is_error=True),
                  ev_assistant(1, text="it failed"), ev_turn_end(1)]
        path = self.write_session(events)
        code, out, _err = self.run_main([path])
        self.assertIn("[tool-error] turn 1", out)

    def test_unanswered_tool_call_is_flagged(self):
        events = [ev_session(), ev_turn_start(1), ev_tool_call(1), ev_turn_end(1)]
        path = self.write_session(events)
        code, out, _err = self.run_main([path])
        self.assertIn("[tool-call-unanswered] turn 1", out)

    def test_finish_length_is_flagged(self):
        events = [ev_session(), ev_turn_start(1), ev_finish(1, "length"),
                  ev_assistant(1, text="partial"), ev_turn_end(1)]
        path = self.write_session(events)
        code, out, _err = self.run_main([path])
        self.assertIn("[finish-length] turn 1", out)

    def test_an_answered_completed_turn_is_not_flagged(self):
        path = self.write_session([ev_session()] + good_turn())
        _code, out, _err = self.run_main([path])
        self.assertIn("anomalies: none detected", out)


class TestOutputModes(DistillCase):
    def test_json_mode_is_valid_and_carries_coverage_and_anomalies(self):
        path = self.write_session([ev_session()] + cutoff_turn())
        code, out, _err = self.run_main([path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("coverage:", payload["coverage"])
        self.assertEqual(payload["anomalies"][0]["flag"], "reasoning-only-stop")
        self.assertEqual(payload["turns"][0]["end_reason"], "completed")

    def test_receipts_mode_emits_cmd_receipt_with_coverage_note(self):
        path = self.write_session([ev_session()] + good_turn())
        code, out, _err = self.run_main([path, "--receipts"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("1 turns", payload["summary"])
        receipt = payload["verified_by"][0]
        self.assertEqual(receipt["kind"], "cmd")
        self.assertIn("--json", receipt["ref"])
        self.assertIn("coverage:", receipt["note"])


class TestInputResolution(DistillCase):
    def test_missing_path_fails_closed_with_a_named_error(self):
        code, _out, err = self.run_main(["/definitely/not/here"])
        self.assertEqual(code, 1)
        self.assertIn("/definitely/not/here", err)

    def test_session_directory_resolves_to_its_log(self):
        self.write_session([ev_session()] + good_turn())
        code, out, _err = self.run_main([self.dir.name])
        self.assertEqual(code, 0)
        self.assertIn("turn 1", out)

    def test_sessions_root_picks_newest_and_states_the_choice(self):
        for name, mtime in (("older", 1000), ("newer", 2000)):
            session_dir = os.path.join(self.dir.name, "root", name)
            os.makedirs(session_dir)
            path = os.path.join(session_dir, "session.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for event in [ev_session(f"session-{name}")] + good_turn():
                    handle.write(json.dumps(event) + "\n")
            os.utime(path, (mtime, mtime))
        code, out, _err = self.run_main([os.path.join(self.dir.name, "root")])
        self.assertEqual(code, 0)
        self.assertIn("session-newer", out)
        self.assertIn("picked newest of 2 sessions", out)

    def test_list_mode_states_coverage(self):
        session_dir = os.path.join(self.dir.name, "root", "a")
        os.makedirs(session_dir)
        with open(os.path.join(session_dir, "session.jsonl"), "w") as handle:
            handle.write(json.dumps(ev_session()) + "\n")
        code, out, _err = self.run_main([os.path.join(self.dir.name, "root"), "--list"])
        self.assertEqual(code, 0)
        self.assertIn("coverage: 1 sessions listed", out)

    def test_unknown_flag_fails_closed(self):
        path = self.write_session([ev_session()] + good_turn())
        code, _out, err = self.run_main([path, "--frobnicate"])
        self.assertEqual(code, 1)
        self.assertIn("--frobnicate", err)


class TestRealShapeFidelity(DistillCase):
    """Shapes real dsh logs actually take — the paths production always uses."""

    def test_injected_context_messages_never_become_the_user_prompt(self):
        # Real turn 1 shape: human message, then injected "plugin" snapshot,
        # then the "skill-catalog" system-reminder. The HUMAN text must win.
        events = [ev_session(), ev_turn_start(1),
                  ev_user("the actual human ask"),
                  ev_user("<runtime-context> injected snapshot", kind="plugin"),
                  ev_user("<system-reminder> A skill is a reusable...", kind="skill-catalog"),
                  ev_assistant(1, text="answered"), ev_turn_end(1)]
        path = self.write_session(events)
        code, out, _err = self.run_main([path])
        self.assertEqual(code, 0)
        self.assertIn("user: the actual human ask", out)
        self.assertNotIn("skill is a reusable", out)

    def test_user_message_without_data_turn_attaches_to_current_turn(self):
        # Real user/message events carry NO data.turn; the current-turn
        # fallback is the path all real logs take.
        events = ([ev_session()] + good_turn(1)
                  + [ev_turn_start(2), ev_user("second ask"),
                     ev_assistant(2, text="second answer"), ev_turn_end(2)])
        path = self.write_session(events)
        code, out, _err = self.run_main([path, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["turns"][1]["user_text"], "second ask")

    def test_multi_step_usage_sums_input_and_reports_final_context(self):
        # One usage chunk per step; in-sum is consumption, final is context size.
        events = [ev_session(), ev_turn_start(1), ev_user("multi"),
                  ev_usage(1, 12202, 55), ev_usage(1, 13082, 148),
                  ev_usage(1, 13157, 288), ev_usage(1, 14042, 280),
                  ev_assistant(1, text="done"), ev_turn_end(1)]
        path = self.write_session(events)
        code, out, _err = self.run_main([path])
        self.assertIn("tokens in-sum 52483 (final ctx 14042) / out 771", out)
        code, out, _err = self.run_main([path, "--json"])
        turn = json.loads(out)["turns"][0]
        self.assertEqual(turn["input_tokens_sum"], 52483)
        self.assertEqual(turn["input_tokens_final_step"], 14042)
        self.assertEqual(turn["output_tokens"], 771)

    def test_resumed_turn_states_both_end_reasons_and_flags_it(self):
        # Real resumed sessions emit turn/end twice for the same turn:
        # interrupted, then (after resume) completed. Neither may vanish.
        events = [ev_session(), ev_turn_start(1), ev_user("ask"),
                  ev_turn_end(1, "interrupted", time=2000),
                  ev_assistant(1, text="answered after resume"),
                  ev_turn_end(1, "completed", time=9000)]
        path = self.write_session(events)
        code, out, _err = self.run_main([path])
        self.assertIn("end: interrupted then completed", out)
        self.assertIn("[turn-resumed] turn 1", out)


class TestCLIGuards(DistillCase):
    """Law 2: contradictory or no-op flag combinations are refused, not ignored."""

    def test_two_output_modes_conflict(self):
        path = self.write_session([ev_session()] + good_turn())
        code, _out, err = self.run_main([path, "--json", "--receipts"])
        self.assertEqual(code, 1)
        self.assertIn("conflict", err)

    def test_tail_with_receipts_is_refused_as_a_noop(self):
        path = self.write_session([ev_session()] + good_turn())
        code, _out, err = self.run_main([path, "--receipts", "--tail", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--tail", err)

    def test_list_with_other_flags_is_refused(self):
        code, _out, err = self.run_main([self.dir.name, "--list", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("--list", err)


class TestReceiptsFidelity(DistillCase):
    def test_receipt_ref_is_replayable_absolute_and_quoted(self):
        session_dir = os.path.join(self.dir.name, "--odd -name")
        os.makedirs(session_dir)
        path = os.path.join(session_dir, "session.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for event in [ev_session()] + good_turn():
                handle.write(json.dumps(event) + "\n")
        code, out, _err = self.run_main([path, "--receipts"])
        ref = json.loads(out)["verified_by"][0]["ref"]
        # The command must replay as-is: shlex round-trip yields exactly the
        # interpreter, the absolute script, the absolute source, and --json —
        # even though the source path contains "--" and a space.
        import shlex as _shlex
        parts = _shlex.split(ref)
        self.assertEqual(parts[0], "python3")
        self.assertEqual(parts[1], os.path.abspath(DISTILL_PATH))
        self.assertEqual(parts[2], os.path.abspath(path))
        self.assertEqual(parts[3], "--json")

    def test_receipts_carry_anomalies(self):
        path = self.write_session([ev_session()] + cutoff_turn())
        code, out, _err = self.run_main([path, "--receipts"])
        payload = json.loads(out)
        self.assertIn("reasoning-only-stop", payload["summary"])
        self.assertEqual(payload["anomalies"][0]["flag"], "reasoning-only-stop")


class TestInputSafety(DistillCase):
    def test_fifo_is_refused_not_hung(self):
        fifo_dir = os.path.join(self.dir.name, "fifodir")
        os.makedirs(fifo_dir)
        fifo = os.path.join(fifo_dir, "session.jsonl")
        os.mkfifo(fifo)
        code, _out, err = self.run_main([fifo_dir])
        self.assertEqual(code, 1)
        self.assertIn("not a regular file", err)

    def test_oversize_input_is_refused_with_the_cap_named(self):
        path = self.write_session([ev_session()] + good_turn())
        os.environ["TEMPLE_DISTILL_MAX_BYTES"] = "10"
        try:
            code, _out, err = self.run_main([path])
        finally:
            del os.environ["TEMPLE_DISTILL_MAX_BYTES"]
        self.assertEqual(code, 1)
        self.assertIn("10-byte cap", err)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_file_fails_closed_naming_the_path(self):
        path = self.write_session([ev_session()] + good_turn())
        os.chmod(path, 0o000)
        try:
            code, _out, err = self.run_main([path])
        finally:
            os.chmod(path, 0o600)
        self.assertEqual(code, 1)
        self.assertIn(path, err)


class TestZstd(DistillCase):
    def _write_zstd(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest("compression.zstd not available on this python")
        plain = self.write_session([ev_session()] + cutoff_turn())
        with open(plain, "rb") as handle:
            compressed = zstd.compress(handle.read())
        zpath = os.path.join(self.dir.name, "session.jsonl.zstd")
        with open(zpath, "wb") as handle:
            handle.write(compressed)
        return zpath

    def test_zstd_roundtrip_when_stdlib_available(self):
        zpath = self._write_zstd()
        code, out, _err = self.run_main([zpath])
        self.assertEqual(code, 0)
        self.assertIn("[reasoning-only-stop] turn 1", out)

    @unittest.skipUnless(__import__("shutil").which("zstd"), "no zstd binary on PATH")
    def test_zstd_binary_fallback_path_works(self):
        # Force the subprocess path by hiding stdlib compression.zstd — this is
        # the LIVE decompression path on every python below 3.14.
        import sys as _sys
        import types as _types
        zpath = self._write_zstd()
        saved = _sys.modules.get("compression")
        _sys.modules["compression"] = _types.ModuleType("compression")  # no .zstd
        try:
            code, out, _err = self.run_main([zpath])
        finally:
            if saved is not None:
                _sys.modules["compression"] = saved
            else:
                del _sys.modules["compression"]
        self.assertEqual(code, 0)
        self.assertIn("[reasoning-only-stop] turn 1", out)


if __name__ == "__main__":
    unittest.main()
