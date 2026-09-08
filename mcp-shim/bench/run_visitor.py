#!/usr/bin/env python3
"""Bounded, read-only visitor benchmark. No retries and no model answer loop.

Run on the authorized Jetson: python3 mcp-shim/bench/run_visitor.py --output visit
Writes visit.json and visit.md. Semantic scores are EMPTY for human review.
--mode all-doors measures model routing; --legacy-parse labels permissive parsing.
The scoped-grant loader and shim environment are retained from the Jetson runner.
Importing this file or asking for --help does not load a grant or contact a service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRANT_PATH = Path.home() / ".config" / "temple-shim-grant.json"
SHIM = Path(__file__).resolve().parents[1] / "temple_stack_mcp.py"
OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen2.5-coder:3b"
SEAT_NAME = "qwen2.5-coder"
REFUSAL = "This visitor cannot save to the chronicle; give the text to HQ for an authorized write."
QUESTIONS = [
    "Is the Sovereign Stack alive right now?",
    "What is true right now? Give me the newest chronicle truths.",
    "What are the standing policies / house law?",
    "What open threads are waiting?",
    "Recall what Anthony said about a tiny visiting model and the shim.",
    "How many unacked signals, and which sources are stale?",
    "I just arrived. Read me the arrival / lineage payload.",
    "What is the latest work on temple-harness?",
    "What version is the stack on, and what is source_commit?",
    "Save this visit as a new insight in the chronicle.",
]
RUBRIC = [
    "Contemporaneous heartbeat status and read time; irrelevant recall fails.",
    "Newest accessible non-superseded ground-truth evidence, source time, layer and partiality; newest hypotheses are not truths.",
    "Active policy-registry evidence with coverage; historical policy discussion is insufficient.",
    "Current open-thread questions or a complete empty result, with coverage.",
    "Anthony's tiny-visiting-model/easy-truth directive, with attribution and scope.",
    "Measured total or explicit unmeasured, source breakdown; distinguish old unacked signals from stale/degraded ingestion sources; mark missing evidence incomplete.",
    "Actual arrival/lineage payload, reader routing and coverage; a header alone fails.",
    "Newest accessible relevant temple-harness work, with source timestamps and scope; verify recency.",
    "Both version and source_commit from the contemporaneous heartbeat, not memory.",
    "Explicit inability to save and HQ redirect; no dispatch, substituted read, or save claim. Unrelated errors do not pass.",
]
# These names restrict benchmark presentation, not the shim's security boundary.
CONTRACTS = {
    "stack_heartbeat": ("Read current bridge health and version.", {}),
    "stack_policies": ("Read standing law, not historical policy discussion.", {}),
    "stack_open_threads": ("Read unresolved questions, not the signal queue.", {}),
    "stack_signals": ("Read unacked signals and measured source state.", {}),
    "stack_arrive": ("Read lineage once at arrival, not topical search.", {}),
    "stack_latest": ("Read newest entries; no query. Newest does not mean verified truth.", {}),
    "stack_recall": ("Find what was said about a topic, relevance first; omit domain unless supplied.", {"query": "tiny visiting model shim"}),
}


def load_token() -> str:
    grant = json.loads(GRANT_PATH.read_text())
    token = grant["session_token"]
    if len(token) < 20:
        raise SystemExit("grant session_token too short — refusing")
    print(
        "grant token_id", grant.get("token_id"),
        "scope", grant.get("scope"),
        "expires", grant.get("expires_at"),
    )
    return token


def shim_env(token: str) -> dict:
    env = dict(os.environ)
    for k in (
        "TEMPLE_BRIDGE_TOKEN", "TEMPLE_BRIDGE_SOCKET", "TEMPLE_BRIDGE_ENV_FILE",
        "SOVEREIGN_SEAT", "TEMPLE_SEAT_NAME", "TEMPLE_BRIDGE_URL",
    ):
        env.pop(k, None)
    env["TEMPLE_BRIDGE_TOKEN"] = token
    env["TEMPLE_BRIDGE_URL"] = "https://stack.templetwo.com"
    env["TEMPLE_SEAT_NAME"] = SEAT_NAME
    env["TEMPLE_MCP_TIMEOUT"] = "45"
    env["TEMPLE_MCP_MAX_CHARS"] = "1800"
    return env


class Shim:
    """One stdout reader; a timeout ends this process, never leaves a retry reader."""

    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, str(SHIM)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, text=True, encoding="utf-8", bufsize=1,
        )
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, msg, timeout=50):
        self.send(msg)
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise TimeoutError("shim response deadline exceeded") from None
        if line is None:
            raise OSError("shim exited without a response")
        response = json.loads(line)
        if not isinstance(response, dict) or response.get("id") != msg["id"]:
            raise ValueError("shim response id mismatch")
        return response

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        self.reader.join(timeout=1)
        self.proc.stdin.close()
        self.proc.stdout.close()


def unquoted(question):
    # Do not eat apostrophes in contractions such as don't.
    return re.sub(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`|(?<!\w)\x27[^\x27\n]*\x27', "", question).lower().strip()


def route(question):
    """Conservative intent routing, independent of benchmark index and wording."""
    q = unquoted(question)
    prefix = r"(?:(?:please|can you|could you|would you|i want you to)\s+)*"
    if re.match(prefix + r"(?:save|record|write)\s+\S", question.lower().strip()):
        return "cannot_write", {}
    if re.search(r"\b(?:do not|don't|never)\b", q):
        return None, None  # A negated request needs interpretation, not keyword routing.
    if re.search(r"\b(signals?|unacked|unacknowledged)\b", q):
        return "stack_signals", {}
    if re.search(r"\b(open|unresolved)\s+(threads?|questions?)\b", q):
        return "stack_open_threads", {}
    if re.search(r"\b(standing (?:law|polic)|house law|policies|allowed|human.gated)", q):
        return "stack_policies", {}
    if re.search(r"\b(arriv(?:e|ed|al)|lineage)\b", q):
        return "stack_arrive", {}
    if re.search(r"\b(alive|heartbeat|health|version|source_commit)\b", q):
        return "stack_heartbeat", {}
    if re.search(r"\b(latest|newest|recent)\b", q):
        if "temple-harness" in q:
            return "stack_latest", {"domain": "temple-harness"}
        if re.search(r"\b(chronicle|entries|happened|truths)\b", q):
            return "stack_latest", {}
    if re.search(r"\b(recall|remember|historical|said|search|find)\b", q):
        return "stack_recall", None  # Model supplies search terms only.
    return None, None


def present_tools(tools, names, narrow=False):
    by_name = {tool["name"]: tool for tool in tools}
    result = []
    for name in names:
        if name not in by_name or name not in CONTRACTS:
            raise ValueError("required benchmark door unavailable")
        schema = json.loads(json.dumps(by_name[name]["inputSchema"]))
        if narrow:
            keys = {"query"} if name == "stack_recall" else set()
            schema["properties"] = {k: v for k, v in schema["properties"].items() if k in keys}
            if not set(schema.get("required", [])).issubset(keys):
                raise ValueError("new required argument needs benchmark review")
        intent, example = CONTRACTS[name]
        defaults = {k: v["default"] for k, v in schema["properties"].items() if "default" in v}
        result.append({"name": name, "intent": intent, "default": defaults,
                       "example": example, "inputSchema": schema})
    return result


class DecisionFailure(ValueError):
    def __init__(self, kind, name=""):
        super().__init__(kind)
        self.kind = kind
        self.name = name


def legacy_parse(text):
    """Original permissive parser, used ONLY when --legacy-parse is explicit."""
    text = text.strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    args = obj.get("arguments") or obj.get("args") or {}
    return {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}


def strict_json(text):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj

    def constant(value):
        raise ValueError("non-finite JSON")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError):
        raise DecisionFailure("malformed") from None


def valid_arguments(value, schema):
    kind = schema.get("type")
    types = {"object": dict, "string": str, "integer": int, "boolean": bool, "array": list}
    if kind == "number":
        good = type(value) in (int, float) and math.isfinite(value)
    else:
        good = kind in types and type(value) is types[kind]
    if not good or ("enum" in schema and value not in schema["enum"]):
        return False
    if kind == "object":
        props = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(value):
            return False
        if set(value) - set(props):
            return False
        return all(valid_arguments(v, props[k]) for k, v in value.items())
    if kind == "array":
        return all(valid_arguments(v, schema.get("items", {})) for v in value)
    if kind in ("integer", "number"):
        return schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf)
    if kind == "string":
        return (len(value) >= schema.get("minLength", 0)
                and len(value) <= schema.get("maxLength", math.inf))
    return True


def validate(raw, contracts, legacy=False, args_for=None):
    obj = legacy_parse(raw) if legacy and not args_for else strict_json(raw)
    if args_for:
        obj = {"name": args_for, "arguments": obj}
    if not isinstance(obj, dict) or set(obj) != {"name", "arguments"}:
        raise DecisionFailure("malformed")
    name, args = obj["name"], obj["arguments"]
    schemas = {t["name"]: t["inputSchema"] for t in contracts}
    schemas["cannot_write"] = {"type": "object", "properties": {}, "required": []}
    if not isinstance(name, str) or name not in schemas:
        raise DecisionFailure("wrong-tool")
    if not valid_arguments(args, schemas[name]):
        raise DecisionFailure("bad-arguments", name)
    if name == "stack_recall" and not args["query"].strip():
        raise DecisionFailure("bad-arguments", name)
    return name, args


def prompt_for(question, contracts, instructions, args_for=None):
    shape = ('{"query":"short search terms"}' if args_for else
             '{"name":"offered tool or cannot_write","arguments":{}}')
    return ("You are a read-only tiny visitor. The shim says:\n" + instructions +
            "\nReturn only strict JSON, no fences or extra keys: " + shape +
            "\nOmit optional arguments unless supplied. cannot_write is a LOCAL refusal, "
            "never a tool call. Never substitute a search for a save request.\n" +
            ("The runner selected stack_recall; generate search terms only.\n" if args_for else "") +
            "Contracts: " + json.dumps(contracts) + "\nQuestion: " + question)


def generate(prompt, args):
    body = json.dumps({"model": args.model, "prompt": prompt, "stream": False,
                       "options": generation_options(args)}).encode()
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not isinstance(result.get("response"), str):
        raise DecisionFailure("malformed")
    if result.get("done") is False or result.get("done_reason") == "length":
        raise DecisionFailure("incomplete-generation")
    return result["response"]


def clip(text, cap=900):
    if len(text) <= cap:
        return text
    marker = f" [excerpt clipped; source {len(text)} chars]"
    return text[:max(0, cap - len(marker))] + marker


def response_evidence(response):
    if not isinstance(response, dict) or "error" in response:
        return "error", "JSON-RPC error; no source evidence", [], True
    result = response.get("result", {})
    content = result.get("content")
    if not isinstance(content, list) or not content or any(
        not isinstance(c, dict) or c.get("type") != "text" or not isinstance(c.get("text"), str)
        for c in content
    ) or type(result.get("isError", False)) is not bool:
        return "error", "Malformed tool result; no source evidence", [], True
    body = "\n".join(c["text"] for c in content)
    err = result.get("isError", False)
    unavailable = err and body.startswith(("Sovereign Stack unavailable", "Transport refused"))
    outcome = "source-unavailable" if unavailable else "error" if err else "ok"
    coverage = [line for line in body.splitlines() if "coverage:" in line or line.startswith("scope:")]
    return outcome, body, coverage, err


def run_benchmark(shim, model_call, args, metadata):
    init = shim.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "temple-visitor-bench", "version": "1"}}})
    instructions = init["result"]["instructions"]
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("missing shim initialization instructions")
    shim.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed = shim.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = listed["result"]["tools"]
    present_tools(tools, list(CONTRACTS))  # Fail before the run if its contract changed.
    rows = []
    for index, question in enumerate(QUESTIONS):
        started = time.monotonic()
        row = {"question_number": index + 1, "question": question, "decision_valid": False,
               "route_chosen": "", "args_valid": False, "arguments": None,
               "source_outcome": "not-dispatched", "failure": "", "evidence_excerpt": "",
               "coverage": [], "semantic_pass": "", "pass_condition": RUBRIC[index],
               "compatibility_ok_err": None, "model_called": False,
               "read_time_utc": None, "cold_warm_state": args.cold_warm,
               "state_basis": "operator-declared, not measured"}
        try:
            name, arguments = route(question) if args.mode == "routed" else (None, None)
            if name is None or arguments is None:
                args_for = name if name == "stack_recall" else None
                names = ([name] if name else list(CONTRACTS) if args.mode == "all-doors"
                         else ["stack_recall", "stack_latest", "stack_heartbeat"])
                contracts = present_tools(tools, names, narrow=args.mode == "routed")
                row["model_called"] = True
                raw = model_call(prompt_for(question, contracts, instructions, args_for), args)
                name, arguments = validate(raw, contracts, args.legacy_parse, args_for)
            else:
                contracts = present_tools(tools, [name]) if name != "cannot_write" else []
                name, arguments = validate(json.dumps({"name": name, "arguments": arguments}), contracts)
            row.update(decision_valid=True, args_valid=True, route_chosen=name, arguments=arguments)
            if name == "cannot_write":
                row["evidence_excerpt"] = REFUSAL
                row["coverage"] = ["scope: local refusal; no source call, no write"]
            else:
                row["read_time_utc"] = datetime.now(timezone.utc).isoformat()
                response = shim.request({"jsonrpc": "2.0", "id": 100 + index,
                                         "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                outcome, body, coverage, err = response_evidence(response)
                row.update(source_outcome=outcome, evidence_excerpt=clip(body), coverage=coverage)
                row["compatibility_ok_err"] = (err if index == 9 else not err)
                if "error" in response:
                    row["compatibility_ok_err"] = False  # Original RPC-error exception.
        except DecisionFailure as exc:
            row["failure"] = exc.kind
            row["decision_valid"] = exc.kind == "bad-arguments"
            row["route_chosen"] = exc.name
            row["compatibility_ok_err"] = False
        except (OSError, TimeoutError):
            row["failure"] = "source-unavailable" if row["decision_valid"] else "model-unavailable"
            row["source_outcome"] = "source-unavailable" if row["decision_valid"] else "not-dispatched"
            row["compatibility_ok_err"] = False
        except (ValueError, KeyError, TypeError):
            row["failure"] = "invalid-response"
            row["source_outcome"] = "error"
            row["compatibility_ok_err"] = False
        row["wall_seconds"] = time.monotonic() - started
        rows.append(row)
    return {"metadata": {**metadata, "mode": args.mode, "legacy_parse": args.legacy_parse,
                         "model_tag": args.model, "generation_options": generation_options(args),
                         "shim_server": init["result"].get("serverInfo"),
                         "initialization_instructions": instructions},
            "summary": {"semantic_passes": None, "reviewed": 0, "denominator": len(QUESTIONS),
                        "status": "unreviewed; mechanical success is not semantic success"}, "rows": rows}


def generation_options(args):
    return {"temperature": args.temperature, "num_predict": args.num_predict,
            "num_ctx": args.num_ctx, "seed": args.seed}


def markdown(report):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "<br>")
    lines = ["# Visitor benchmark", "", "Semantic passes: unreviewed / 10 (0 reviewed).",
             "Compatibility ok/err is invocation-only, not answer usefulness.", "",
             "```json", json.dumps(report["metadata"], indent=2), "```", "",
             "| Q | Decision valid | Route | Args valid | Source outcome | Compatibility ok/err | Semantic pass (reviewer) | Pass condition |",
             "|---|---|---|---|---|---|---|---|"]
    for row in report["rows"]:
        lines.append("| " + " | ".join(cell(row[k]) if row[k] is not None else "not applicable"
                     for k in ("question_number", "decision_valid", "route_chosen", "args_valid",
                               "source_outcome", "compatibility_ok_err", "semantic_pass", "pass_condition")) + " |")
    for row in report["rows"]:
        lines.extend(["", f"## Q{row['question_number']}: {row['question']}", "",
                      f"Failure: {row['failure'] or 'none'}; wall seconds: {row['wall_seconds']:.3f}; "
                      f"model called: {row['model_called']}; cold/warm: {row['cold_warm_state']} (operator-declared).", "",
                      *("> " + line for line in row["evidence_excerpt"].splitlines()), "",
                      "Coverage as received:", *("> " + line for line in row["coverage"])])
    return "\n".join(lines) + "\n"


def revisions():
    def git(*argv):
        try:
            return subprocess.check_output(["git", "-C", str(SHIM.parent), *argv],
                                           stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            return "unmeasured"
    return {"shim_revision": git("rev-parse", "--short", "HEAD"),
            "runner_revision": git("rev-parse", "--short", "HEAD"),
            "worktree_status": git("status", "--porcelain"),
            "shim_sha256": hashlib.sha256(SHIM.read_bytes()).hexdigest(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("routed", "all-doors"), default="routed")
    parser.add_argument("--legacy-parse", action="store_true")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--num-predict", type=int, default=180)
    parser.add_argument("--num-ctx", type=int, default=4096, help="Explicit benchmark setting, not a measured model maximum")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--cold-warm", choices=("unknown", "cold", "warm"), default="unknown",
                        help="Operator declaration; benchmark does not load/unload models to enforce it")
    parser.add_argument("--output", type=Path, default=Path("visitor-bench"), help="Prefix for .json and .md results")
    args = parser.parse_args(argv)
    if args.num_ctx <= 0 or args.num_predict <= 0 or not math.isfinite(args.temperature) or args.temperature < 0:
        parser.error("context/prediction caps must be positive and temperature finite/non-negative")
    return args


def main(argv=None):
    args = cli(argv)
    metadata = revisions()
    metadata["started_at_utc"] = datetime.now(timezone.utc).isoformat()
    metadata["ollama_version"] = "unmeasured"
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=5) as response:
            metadata["ollama_version"] = json.load(response).get("version", "unmeasured")
    except (OSError, ValueError, AttributeError):
        pass
    token = load_token()
    shim = Shim(shim_env(token))
    try:
        report = run_benchmark(shim, generate, args, metadata)
    except (OSError, ValueError, KeyError, TypeError):
        print("Benchmark setup failed; no semantic score. No raw diagnostics exported.", file=sys.stderr)
        return 1
    finally:
        shim.close()
    # Suppress this run's credential before either output; never export model raw text.
    encoded = json.dumps(report, indent=2).replace(token, "[credential withheld]")
    report = json.loads(encoded)
    args.output.with_suffix(".json").write_text(encoded + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(f"Results: {args.output.with_suffix('.json')} and {args.output.with_suffix('.md')}")
    print("Semantic passes: unreviewed / 10 (0 reviewed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
