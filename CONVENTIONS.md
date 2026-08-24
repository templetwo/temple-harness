# Temple-Harness Conventions

These are the written laws of this repo. They were practiced before they were
written; writing them makes them enforceable. Where a law can be checked by a
test, it is — `mcp-shim/tests/test_conventions.py` and the log-distill suite
go red when one is broken. A convention without an enforcement point is listed
as such, honestly.

The standing test, verbatim, binding on every proposed addition to this repo:

> "Does this materially improve the daily companion, or does it materially
> improve honesty? If neither, defer it."

The burden is on justifying another refinement cycle, not on justifying
stopping.

## 1. Coverage is always stated

Every tool result and every distilled output says what it is showing out of
what exists: `returned 5 of 894 matched | bridge-side truncated`,
`turns 2 of 9 shown`, `X events read, Y unparseable`. Two different
truncations (source-side and our own character cap) are reported separately
and never conflated. A result that silently drops content is the house
anti-pattern everything here exists downstream of.

Enforced: `test_conventions.py` calls every shim tool and asserts a coverage
or scope line; the log-distill suite asserts the coverage line in every
output mode.

## 2. Fail closed, speak plainly, redirect

Unknown or invalid input is refused loudly with a message that names the
right door (`stack_latest takes no 'query' … use stack_recall`). Errors are
never dressed as empty successes; an unreachable source says so in the
result. Inspection surfaces (`--dump-config`) work without credentials, but
serving surfaces refuse to start without them.

Enforced: the shim's fail-closed test family; the redirect asserted in the
stack_latest tests; log-distill's input-safety tests (missing, unreadable,
non-regular, and oversize inputs) and CLI-guard tests (contradictory flag
combinations refused).

## 3. The boundary is a diff, not a runtime decision

Read/write scope lives in module-level constants: `ALLOWED_BRIDGE_TOOLS` and
`ALLOWED_BRIDGE_PATHS` are frozensets; `BRIDGE_TARGETS` is the door map,
pinned to those allowlists by the exact-mapping test. Widening scope requires
editing those constants — a reviewable diff — and can never happen via
prompt, argument, or configuration. The write lane for local models does not exist
until Anthony rules otherwise; that ruling is untaken.

Enforced: the read-only-boundary test family, including the exact-mapping
test and the refusal-before-network test. Negative controls are part of
review discipline: mutate each gate, watch the suite go red, restore.

## 4. Legible to a 9B reader

Our models are small and scarred (documented: tool-result bleed into task
representation, reasoning-only stops, groove-lock). Therefore: tool
descriptions stay short and carry ROUTING ("use this ONLY for…", "for X use
Y instead"); schemas close with `additionalProperties: false`; caps are
stated; truncation is announced with both numbers. If a 9B cannot decide
which door to take from the description alone, the description is wrong.

Enforced: `test_conventions.py` caps description length and requires routing
language on overlapping doors; schema-strictness asserted for every tool.

## 5. Stdlib only, single file, readable end-to-end

The Temple layer carries no pip installs, no Node, no build step, and no
dynamic code loading. One person must be able to read any component in one
sitting. A smaller tool we can read end-to-end beats a larger one we trust.

Enforced by review (no mechanical check; listed honestly).

## 6. We own the audit layer, not the loop

temple-harness reads and distills the host harness's records (sessions,
configs); it does not reimplement the agent loop, and it does not write the
host's logs. Own the thin honest surfaces — memory access, skills, log
distillation, config inspection — and ride the current harness for the rest.
(Adopted 2026-08-24 from the dsh absorption review, Grok Heavy's outside
opinion concurring.)

Enforced by the standing test at review time.

## 7. Nothing log-derived travels unredacted

Host session logs are where pasted keys end up, and distilled output is
built to travel — into chronicles, reports, and chats. Every string
log-distill emits passes through one funnel (`_clip`), and that funnel masks
credential-shaped content before anything leaves (`_redact`, with the marker
stated in place of the bytes). The same standard the t2fathom card layer
already held: the audit layer must never become the exfiltration layer.

Enforced: `RedactionTests` in the log-distill suite — redaction fires at the
funnel, survives clipping, and is proven load-bearing (empty the patterns
and a probe leaks, so the gate is real, not incidental). (HQ amendment,
2026-08-24, from a live incident on the Studio.)
