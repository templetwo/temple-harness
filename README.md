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

## Boundaries

The shim is **read-only by design**: recall, open threads, heartbeat. The write
lane (record_insight etc.) stays with gated seats; whether local models ever get
write scope is Anthony's ruling, untaken. Steps 3–4 of the personalization
ladder (imatrix requant, LoRA on the relational archive) are likewise unruled
and out of scope here.
