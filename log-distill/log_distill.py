#!/usr/bin/env python3
"""log-distill — turn a dsh session event log into a legible, honest summary.

The DeepSeek Harness (dsh) writes append-only session event logs (JSONL,
usually zstd-compressed): every user message, reasoning chunk, tool call,
token count, and finish reason, with sequence numbers. That log is the
source of truth for what a model actually saw and did. This tool is the
Temple's READER for it — it does not write logs, because we do not own the
loop that produces them; it distills the host harness's own record into:

  * a human-and-9B-legible summary (turns, tool calls, finish reasons,
    token usage, timing), and
  * anomaly flags for the failure modes this house has already paid to
    diagnose by hand:
      - reasoning-only-stop: a turn that ended "completed" whose final
        assistant message contains reasoning but NO answer text (the Q2
        scar: the model thought, then emitted end-of-turn instead of the
        answer),
      - turn-error: a turn/end carrying a structured error (e.g.
        UNKNOWN_MODEL),
      - tool-error: a tool result flagged isError,
      - finish-length: a generation cut by token limit.

Design constraints, inherited from the house (see CONVENTIONS.md):

  * STDLIB ONLY, single file. zstd via compression.zstd (python 3.14+),
    falling back to the `zstd` binary, else a plain error naming both.
  * COVERAGE ALWAYS STATED. Every output mode ends with a coverage line:
    events read, lines that failed to parse, turns found, event types this
    tool did not recognize. A distiller that hides what it skipped is the
    exact failure this house exists downstream of.
  * FAIL CLOSED, SPEAK PLAINLY. Unknown event types are counted and named,
    never silently dropped; unreadable input is an error naming the path,
    never an empty success.
  * READ-ONLY. This tool opens files and writes stdout. Nothing else.

Usage:
  log_distill.py PATH [--json] [--receipts] [--tail N] [--max-text M]
  log_distill.py ROOT --list

  PATH may be a session.jsonl / session.jsonl.zstd file, a session
  directory containing one, or a sessions root (newest session is picked,
  and the choice is stated). --list enumerates sessions under a root.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys

TOOL_NAME = "log-distill"
TOOL_VERSION = "0.1.0"

DEFAULT_MAX_TEXT = 200

# Refuse inputs larger than this (compressed size) rather than exhaust memory
# on a hostile or corrupt log. Override with TEMPLE_DISTILL_MAX_BYTES.
DEFAULT_MAX_INPUT_BYTES = 256 * 1024 * 1024


def max_input_bytes() -> int:
    raw = os.environ.get("TEMPLE_DISTILL_MAX_BYTES")
    try:
        value = int(raw) if raw else DEFAULT_MAX_INPUT_BYTES
    except (TypeError, ValueError):
        return DEFAULT_MAX_INPUT_BYTES
    return value if value > 0 else DEFAULT_MAX_INPUT_BYTES

# Event types this distiller understands. Anything outside this set is
# counted and reported in the coverage line — never silently dropped.
KNOWN_EVENT_TYPES = frozenset({
    "session", "session/title", "session/title-llm-request", "session/end-seed",
    "request/header", "request/context",
    "permission/preset", "sandbox/mode", "approval/policy",
    "turn/start", "turn/end", "step/start", "step/end",
    "user/message", "assistant/message", "assistant/chunk",
    "reasoning-chunks", "text-chunks",
    "tool/call", "tool/result",
    "agent/inbox/spliced",
})


# --------------------------------------------------------------------------
# Input: locate and decompress
# --------------------------------------------------------------------------


class DistillError(Exception):
    """Input could not be located or read. Message names the path and why."""


def find_session_files(root: str) -> list:
    """All session.jsonl[.zstd] files under root, newest mtime first."""
    hits = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name in ("session.jsonl", "session.jsonl.zstd"):
                path = os.path.join(dirpath, name)
                try:
                    hits.append((os.path.getmtime(path), path))
                except OSError:
                    continue
    hits.sort(reverse=True)
    return [path for _mtime, path in hits]


def resolve_input(path: str) -> tuple:
    """Resolve PATH to one session log file.

    Returns (file_path, note) where note says how the choice was made —
    the choice is always stated, never silent.
    """
    if os.path.isfile(path):
        return path, None
    if os.path.isdir(path):
        direct = [os.path.join(path, n) for n in ("session.jsonl.zstd", "session.jsonl")]
        for candidate in direct:
            if os.path.isfile(candidate):
                return candidate, None
        found = find_session_files(path)
        if not found:
            raise DistillError(f"no session.jsonl[.zstd] found under {path}")
        note = f"picked newest of {len(found)} sessions under {path}"
        return found[0], note
    raise DistillError(f"no such file or directory: {path}")


def read_log_bytes(path: str) -> bytes:
    """Read the log, decompressing zstd via stdlib or the zstd binary.

    Refuses non-regular files (a FIFO would hang forever) and files over the
    size cap (a corrupt or hostile log must not exhaust memory) — both with
    plain errors naming the path and the reason.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise DistillError(f"cannot stat {path} — {exc}") from None
    if not stat.S_ISREG(mode):
        raise DistillError(f"{path} is not a regular file — refusing to read it")
    size = os.path.getsize(path)
    cap = max_input_bytes()
    if size > cap:
        raise DistillError(
            f"{path} is {size} bytes, over the {cap}-byte cap "
            "(raise TEMPLE_DISTILL_MAX_BYTES if this log is really that big)"
        )
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DistillError(f"cannot read {path} — {exc}") from None
    if not path.endswith(".zstd"):
        return raw
    try:
        from compression import zstd  # python 3.14+ stdlib (PEP 784)
        return zstd.decompress(raw)
    except ImportError:
        pass
    except Exception as exc:  # corrupt input surfaces plainly, not as a crash
        raise DistillError(f"zstd decompress failed for {path} — {exc}") from None
    try:
        # `--` ends option parsing: dsh session directories legitimately start
        # with "--" (workspace-encoded names), so a relative path could
        # otherwise be read as options by the zstd binary.
        proc = subprocess.run(["zstd", "-dc", "--", path], capture_output=True, check=True)
        return proc.stdout
    except FileNotFoundError:
        raise DistillError(
            f"{path} is zstd-compressed but neither compression.zstd (python 3.14+) "
            "nor a `zstd` binary is available"
        ) from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise DistillError(f"zstd -dc failed for {path} — {detail}") from None


# --------------------------------------------------------------------------
# Parsing: events -> turns
# --------------------------------------------------------------------------


# Credential-shaped strings are masked before ANY text leaves this tool.
# The host logs this reader distills are exactly where pasted keys end up
# (a real key sat in a real session transcript for six days before a
# receipted dive found it, 2026-08-24). Distilled output is built to travel
# — chronicles, reports, chats — so the mask is applied at the single funnel
# every emitted string passes through, and the marker states what happened
# without carrying a byte of the secret. High-precision patterns only; a
# false mask costs a few readable characters, a false pass costs a rotation.
_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"xai-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
]
_REDACT_MARKER = "[REDACTED:key-shaped]"


def _redact(text: str) -> str:
    """Mask credential-shaped substrings, stating so in place."""
    for pat in _REDACT_PATTERNS:
        text = pat.sub(_REDACT_MARKER, text)
    return text


def _clip(text: str, cap: int) -> str:
    """Redact, then cap and SAY SO — the marker is appended, never itself cut."""
    text = _redact(" ".join(str(text or "").split()))
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + f" [+{len(text) - cap} chars]"


def _message_texts(message: dict) -> tuple:
    """Split an assistant/user message's content into (text, reasoning) strings."""
    text_parts, reasoning_parts = [], []
    for block in (message or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "reasoning":
            reasoning_parts.append(str(block.get("text") or ""))
    return "".join(text_parts).strip(), "".join(reasoning_parts).strip()


def parse_session(raw: bytes) -> dict:
    """One pass over the JSONL: build turns, count what was not understood."""
    turns = {}          # turn number -> accumulating dict
    order = []          # turn numbers in first-seen order
    unknown = {}        # unknown event type -> count
    events = 0
    unparseable = 0
    session_meta = {}

    def turn_for(number):
        if number not in turns:
            turns[number] = {
                "turn": number, "user_text": "", "steps": {}, "tool_calls": [],
                "assistant_text": "", "assistant_reasoning_chars": 0,
                "finish_kinds": [], "input_tokens_final": None,
                "input_tokens_sum": 0, "output_tokens": 0,
                "end_reason": None, "end_reasons": [], "end_error": None,
                "start_time": None, "end_time": None,
            }
            order.append(number)
        return turns[number]

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        events += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            unparseable += 1
            continue
        if not isinstance(event, dict):
            unparseable += 1
            continue

        etype = event.get("type") or "(untyped)"
        if etype not in KNOWN_EVENT_TYPES:
            unknown[etype] = unknown.get(etype, 0) + 1
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}

        if etype == "session":
            session_meta = {
                "id": event.get("id"), "cwd": event.get("cwd"),
                "createdAt": event.get("createdAt"),
            }
        elif etype == "turn/start":
            turn = turn_for(data.get("turn"))
            turn["start_time"] = event.get("time")
        elif etype == "turn/end":
            turn = turn_for(data.get("turn"))
            turn["end_time"] = event.get("time")
            reason = data.get("reason") or {}
            # Resumed sessions re-run a turn and emit a SECOND turn/end. Keep
            # every reason so an earlier "interrupted" is stated, never lost.
            if reason.get("kind"):
                turn["end_reasons"].append(reason.get("kind"))
            turn["end_reason"] = reason.get("kind")
            if isinstance(reason.get("error"), dict):
                err = reason["error"]
                turn["end_error"] = {"message": err.get("message"), "code": err.get("code")}
        elif etype == "user/message":
            # Real dsh emits several user/message events per human turn: the
            # human's (source.kind "user"), then injected "plugin" and
            # "skill-catalog" context. Only the HUMAN one is the prompt —
            # accepting the others reports the skills listing as the user.
            source_kind = (data.get("source") or {}).get("kind")
            if source_kind != "user":
                continue
            # Real user/message events carry no data.turn; attach to the
            # current (latest-started) turn.
            number = data.get("turn")
            if number is None:
                number = order[-1] if order else 1
            turn = turn_for(number)
            text, _reasoning = _message_texts(data)
            if text:
                turn["user_text"] = text
        elif etype == "assistant/message":
            turn = turn_for(data.get("turn"))
            text, reasoning = _message_texts(data.get("message") or {})
            # keep the LAST assistant text of the turn (the final answer slot)
            turn["assistant_text"] = text
            turn["assistant_reasoning_chars"] = len(reasoning)
        elif etype == "assistant/chunk":
            turn = turn_for(data.get("turn"))
            chunk = data.get("chunk") or {}
            if chunk.get("type") == "usage":
                # One usage chunk per STEP: inputTokens is that step's full
                # context. The sum is what the turn consumed (cost); the final
                # value is the context the turn grew to. Report both, labeled.
                usage = chunk.get("usage") or {}
                if usage.get("inputTokens") is not None:
                    turn["input_tokens_final"] = usage["inputTokens"]
                    turn["input_tokens_sum"] += usage["inputTokens"]
                turn["output_tokens"] += usage.get("outputTokens") or 0
            elif chunk.get("type") == "finish":
                kind = (chunk.get("reason") or {}).get("kind")
                if kind:
                    turn["finish_kinds"].append(kind)
        elif etype == "tool/call":
            turn = turn_for(data.get("turn"))
            args = str(data.get("arguments") or "")
            turn["tool_calls"].append({
                "name": data.get("name"), "callId": data.get("callId"),
                "args_chars": len(args), "args_preview": args,
                "result_chars": None, "is_error": None,
            })
        elif etype == "tool/result":
            turn = turn_for(data.get("turn"))
            message = data.get("message") or {}
            call_id, is_error, result_chars = None, None, 0
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool-result":
                    call_id = block.get("toolCallId")
                    is_error = bool(block.get("isError"))
                    for inner in block.get("content") or []:
                        if isinstance(inner, dict) and inner.get("type") == "text":
                            result_chars += len(str(inner.get("text") or ""))
            for call in turn["tool_calls"]:
                if call["callId"] == call_id or (call_id is None and call["result_chars"] is None):
                    call["result_chars"] = result_chars
                    call["is_error"] = is_error
                    break
        # step/start, step/end, chunks streams, and admin events need no
        # per-turn state beyond what the events above already carry.

    return {
        "session": session_meta,
        "turns": [turns[number] for number in order],
        "events": events,
        "unparseable": unparseable,
        "unknown_types": unknown,
    }


# --------------------------------------------------------------------------
# Anomalies — the flags this house has already paid to learn
# --------------------------------------------------------------------------


def detect_anomalies(parsed: dict) -> list:
    flags = []
    for turn in parsed["turns"]:
        number = turn["turn"]
        if len(turn["end_reasons"]) > 1:
            flags.append({
                "flag": "turn-resumed", "turn": number,
                "detail": (
                    "this turn ended more than once ("
                    + " then ".join(turn["end_reasons"])
                    + ") — the session was interrupted and resumed; durations span the gap"
                ),
            })
        if turn["end_error"]:
            flags.append({
                "flag": "turn-error", "turn": number,
                "detail": f"{turn['end_error'].get('code')}: {turn['end_error'].get('message')}",
            })
        if turn["end_reason"] == "completed" and turn["assistant_reasoning_chars"] > 0 \
                and not turn["assistant_text"]:
            flags.append({
                "flag": "reasoning-only-stop", "turn": number,
                "detail": (
                    f"turn completed with {turn['assistant_reasoning_chars']} chars of "
                    "reasoning and NO answer text — the model thought, then stopped"
                ),
            })
        for kind in turn["finish_kinds"]:
            if kind == "length":
                flags.append({
                    "flag": "finish-length", "turn": number,
                    "detail": "a generation in this turn was cut by token limit",
                })
        for call in turn["tool_calls"]:
            if call["is_error"]:
                flags.append({
                    "flag": "tool-error", "turn": number,
                    "detail": f"tool '{call['name']}' returned isError",
                })
            if call["result_chars"] is None:
                flags.append({
                    "flag": "tool-call-unanswered", "turn": number,
                    "detail": f"tool '{call['name']}' has no matching result in the log",
                })
    return flags


# --------------------------------------------------------------------------
# Rendering — coverage always stated
# --------------------------------------------------------------------------


def coverage_line(parsed: dict, source: str, note, shown_turns: int) -> str:
    total_turns = len(parsed["turns"])
    parts = [
        f"coverage: {parsed['events']} events read, {parsed['unparseable']} unparseable",
        f"turns {shown_turns} of {total_turns} shown",
    ]
    if parsed["unknown_types"]:
        listed = ", ".join(f"{k}×{v}" for k, v in sorted(parsed["unknown_types"].items()))
        parts.append(f"unrecognized event types: {listed}")
    else:
        parts.append("all event types recognized")
    parts.append(f"source: {source}")
    if note:
        parts.append(note)
    return " | ".join(parts)


def render_text(parsed: dict, flags: list, source: str, note, tail, max_text: int) -> str:
    turns = parsed["turns"][-tail:] if tail else parsed["turns"]
    lines = [f"dsh session distilled — {TOOL_NAME} {TOOL_VERSION}"]
    meta = parsed["session"]
    if meta:
        lines.append(f"session: {meta.get('id')} | cwd: {meta.get('cwd')}")
    lines.append("")

    for turn in turns:
        duration = ""
        if turn["start_time"] and turn["end_time"]:
            duration = f", {round((turn['end_time'] - turn['start_time']) / 1000)}s"
        ends = turn["end_reasons"] or ([turn["end_reason"]] if turn["end_reason"] else [])
        end_text = " then ".join(ends) if len(ends) > 1 else (ends[0] if ends else "(no turn/end)")
        header = f"turn {turn['turn']} — end: {end_text}"
        if turn["input_tokens_final"] is not None:
            header += (
                f", tokens in-sum {turn['input_tokens_sum']}"
                f" (final ctx {turn['input_tokens_final']}) / out {turn['output_tokens']}"
            )
        header += duration
        lines.append(header)
        if turn["user_text"]:
            lines.append(f"  user: {_clip(turn['user_text'], max_text)}")
        for call in turn["tool_calls"]:
            result = "no result" if call["result_chars"] is None else (
                f"error result" if call["is_error"] else f"{call['result_chars']} chars"
            )
            lines.append(
                f"  tool: {call['name']}({_clip(call['args_preview'], 80)}) -> {result}"
            )
        if turn["assistant_reasoning_chars"]:
            lines.append(f"  reasoning: {turn['assistant_reasoning_chars']} chars")
        if turn["assistant_text"]:
            lines.append(f"  answer: {_clip(turn['assistant_text'], max_text)}")
        elif turn["end_reason"] == "completed":
            lines.append("  answer: (none)")
        if turn["end_error"]:
            lines.append(f"  ERROR: {turn['end_error'].get('code')}: "
                         f"{_clip(turn['end_error'].get('message') or '', max_text)}")
        lines.append("")

    if flags:
        lines.append("anomalies:")
        for flag in flags:
            lines.append(f"  [{flag['flag']}] turn {flag['turn']}: {flag['detail']}")
    else:
        lines.append("anomalies: none detected")
    lines.append("")
    lines.append(coverage_line(parsed, source, note, len(turns)))
    return "\n".join(lines)


def render_json(parsed: dict, flags: list, source: str, note, tail, max_text: int) -> str:
    turns = parsed["turns"][-tail:] if tail else parsed["turns"]
    payload = {
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "session": parsed["session"],
        "turns": [
            {
                "turn": t["turn"], "end_reason": t["end_reason"],
                "end_error": t["end_error"],
                "user_text": _clip(t["user_text"], max_text),
                "tool_calls": [
                    {"name": c["name"], "result_chars": c["result_chars"],
                     "is_error": c["is_error"]}
                    for c in t["tool_calls"]
                ],
                "assistant_text": _clip(t["assistant_text"], max_text),
                "assistant_reasoning_chars": t["assistant_reasoning_chars"],
                "finish_kinds": t["finish_kinds"], "end_reasons": t["end_reasons"],
                "input_tokens_final_step": t["input_tokens_final"],
                "input_tokens_sum": t["input_tokens_sum"],
                "output_tokens": t["output_tokens"],
            }
            for t in turns
        ],
        "anomalies": flags,
        "coverage": coverage_line(parsed, source, note, len(turns)),
    }
    return json.dumps(payload, indent=2)


def render_receipts(parsed: dict, flags: list, source: str, note, tail, max_text: int) -> str:
    """A chronicle-ready block: one-line summary + a cmd receipt, as JSON."""
    turns = parsed["turns"]
    flag_names = sorted({f["flag"] for f in flags})
    summary = (
        f"dsh session {parsed['session'].get('id') or '(unknown)'}: "
        f"{len(turns)} turns, "
        f"{sum(len(t['tool_calls']) for t in turns)} tool calls, "
        f"{sum(t['output_tokens'] for t in turns)} output tokens"
    )
    summary += f"; anomalies: {', '.join(flag_names)}" if flag_names else "; no anomalies"
    # The receipt must be REPLAYABLE as emitted: absolute script path, quoted
    # source path (dsh session directories contain "--" and spaces are legal).
    script = os.path.abspath(__file__)
    payload = {
        "summary": summary,
        "verified_by": [{
            "kind": "cmd",
            "ref": f"python3 {shlex.quote(script)} {shlex.quote(os.path.abspath(source))} --json",
            "note": coverage_line(parsed, source, note, len(turns)),
        }],
        "anomalies": flags,
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _usage(stream) -> None:
    stream.write(
        "usage: log_distill.py PATH [--json | --receipts] [--tail N] [--max-text M]\n"
        "       log_distill.py ROOT --list\n"
        "PATH: a session.jsonl[.zstd] file, a session directory, or a sessions root\n"
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        _usage(sys.stderr if not argv else sys.stdout)
        return 0 if argv else 1
    if argv[0] == "--version":
        sys.stdout.write(f"{TOOL_NAME} {TOOL_VERSION}\n")
        return 0

    path = argv[0]
    flags_argv = argv[1:]
    mode = "text"
    tail = None
    max_text = DEFAULT_MAX_TEXT
    listing = False

    index = 0
    mode_flags = []
    while index < len(flags_argv):
        flag = flags_argv[index]
        if flag == "--json":
            mode = "json"
            mode_flags.append(flag)
        elif flag == "--receipts":
            mode = "receipts"
            mode_flags.append(flag)
        elif flag == "--list":
            listing = True
        elif flag in ("--tail", "--max-text"):
            index += 1
            if index >= len(flags_argv):
                sys.stderr.write(f"{TOOL_NAME}: {flag} requires a number\n")
                return 1
            try:
                value = int(flags_argv[index])
            except ValueError:
                sys.stderr.write(f"{TOOL_NAME}: {flag} requires a number, got {flags_argv[index]!r}\n")
                return 1
            if value <= 0:
                sys.stderr.write(f"{TOOL_NAME}: {flag} must be positive\n")
                return 1
            if flag == "--tail":
                tail = value
            else:
                max_text = value
        else:
            sys.stderr.write(f"{TOOL_NAME}: unknown flag {flag!r}\n")
            _usage(sys.stderr)
            return 1
        index += 1

    # Fail closed on contradictory or no-op combinations rather than silently
    # honoring one flag and ignoring the other (CONVENTIONS.md law 2).
    if len(mode_flags) > 1:
        sys.stderr.write(f"{TOOL_NAME}: {' and '.join(mode_flags)} conflict — pick one output mode\n")
        return 1
    if mode == "receipts" and tail is not None:
        sys.stderr.write(
            f"{TOOL_NAME}: --tail has no effect with --receipts (receipts summarize the whole "
            "session) — drop one\n"
        )
        return 1
    if listing and (mode_flags or tail is not None):
        sys.stderr.write(f"{TOOL_NAME}: --list takes no other flags\n")
        return 1

    try:
        if listing:
            found = find_session_files(path) if os.path.isdir(path) else []
            if not found:
                sys.stderr.write(f"{TOOL_NAME}: no sessions found under {path}\n")
                return 1
            for session_path in found:
                sys.stdout.write(session_path + "\n")
            sys.stdout.write(f"coverage: {len(found)} sessions listed | source: {path}\n")
            return 0

        file_path, note = resolve_input(path)
        parsed = parse_session(read_log_bytes(file_path))
        anomaly_flags = detect_anomalies(parsed)
        renderer = {"text": render_text, "json": render_json, "receipts": render_receipts}[mode]
        sys.stdout.write(renderer(parsed, anomaly_flags, file_path, note, tail, max_text) + "\n")
        return 0
    except DistillError as exc:
        sys.stderr.write(f"{TOOL_NAME}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
