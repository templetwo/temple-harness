# temple-harness

Shared harness-layer assets for the Temple of Two's seats — one skill set that
both Claude Code and the DeepSeek Harness (dsh) inject, plus the local MCP shim
that gives dsh-driven local models read access to the Sovereign Stack chronicle.

## Layout

- `skills/` — SKILL.md skills distilled from the chronicle's standing lessons.
  Identical format across harnesses (proven 2026-08-23: dsh auto-discovers
  Claude Code SKILL.md files). Sync this directory to any seat; point both
  harnesses at it.
  - `verify-before-declaring` · `receipts-discipline` · `supersession-ethic` · `register-matching`
- `mcp-shim/` — a thin local MCP (stdio) server wrapping the Sovereign Stack's
  REST bridge, read-only scope, so any local model running under dsh gains
  chronicle recall. Personalization through shared memory, not changed weights.
  `--dump-config` prints the fully resolved configuration (doors, allowlists,
  token presence — never the value) without serving.
- `log-distill/` — a single-file reader for dsh's append-only session event
  logs (JSONL/zstd). Distills a session into a legible summary with anomaly
  flags for the failure modes this house has diagnosed by hand
  (reasoning-only-stop, turn-error, tool-error, finish-length), plus `--json`
  and a chronicle-ready `--receipts` mode. We own the audit layer, not the
  loop: this READS the host harness's record, it never writes one.
- `CONVENTIONS.md` — the written laws of this repo (coverage always stated,
  fail closed with redirect, boundary-is-a-diff, legible to a 9B, stdlib
  only). Where a law is testable, `test_conventions.py` and the log-distill
  suite go red when it breaks.

## Boundaries

The shim is **read-only by design**: recall, open threads, heartbeat. The write
lane (record_insight etc.) stays with gated seats; whether local models ever get
write scope is Anthony's ruling, untaken. Steps 3–4 of the personalization
ladder (imatrix requant, LoRA on the relational archive) are likewise unruled
and out of scope here.
