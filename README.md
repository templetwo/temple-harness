# temple-harness

Shared harness-layer assets for the Temple of Two's seats — one skill set that
both Claude Code and the DeepSeek Harness (dsh) inject, plus the local MCP shim
that gives dsh-driven local models read access to the Sovereign Stack chronicle.

## Layout

- `skills/` — SKILL.md skills distilled from the chronicle's standing lessons.
  Identical format across harnesses. Observed 2026-08-23: dsh auto-discovers
  Claude Code SKILL.md files (no receipt is checked in here). Sync this
  directory to any seat; point both harnesses at it.
  - `verify-before-declaring` · `receipts-discipline` · `supersession-ethic` · `register-matching`
- `mcp-shim/` — a thin local MCP (stdio) server wrapping the Sovereign Stack's
  REST bridge, read-only scope, so any local model running under dsh gains
  chronicle recall, arrival, standing law and the unacked watch queue.
  Personalization through shared memory, not changed weights. Two transports,
  chosen by configuration and never guessed: the Studio seat socket (no
  credential at all) or a scoped grant. It never carries the master key.
  `--dump-config` prints the fully resolved configuration (transport and why,
  doors, allowlists — never a value) without serving.
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

The shim is **read-only by design** — the doors are named, not counted, because
a count rots (`stack_recall`, `stack_latest`, `stack_open_threads`,
`stack_arrive`, `stack_policies`, `stack_signals`, `stack_heartbeat`). The write
lane (record_insight etc.) stays with gated seats; whether local models ever get
write scope is Anthony's ruling, untaken. The seat socket makes a Studio-side
write lane technically available — a seated terminal reaches the stack's write
tools with no credential at all — and the ruling is still his. Steps 3–4 of the
personalization ladder (imatrix requant, LoRA on the relational archive) are
likewise unruled and out of scope here.

The shim also **never carries the master bridge key**. Anthony's rule of
2026-09-05: inside the Studio, seats he seated use no tokens; outside it, a seat
asks for a scoped grant. The env-file fallback that loaded that key by default is
removed, and the variable that named it is refused rather than ignored.
