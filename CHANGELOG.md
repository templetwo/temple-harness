# Changelog

## 0.4.0 — stack readiness (PR: feat/stack-readiness-2026-09-06)

Built against the Sovereign Stack as deployed 2026-09-06 (52 published tools, 48
retired; none of this shim's doors among them).

### mcp-shim 0.4.0

**Credential path — TIER TWO (law 8), Anthony's tap.**

- Two transports, chosen by configuration and never by guessing. **Seat socket:**
  `SOVEREIGN_SEAT` (or `TEMPLE_BRIDGE_SOCKET`) sends `X-Sovereign-Seat` over
  HTTP/1.1 on `AF_UNIX` with **no `Authorization` header** — the bridge routes any
  Authorization header to the bearer check, so sending both would silently stop
  being a seat. **Scoped grant:** `TEMPLE_BRIDGE_TOKEN` only.
- **The env-file fallback is REMOVED.** `parse_env_file`, `load_token`,
  `DEFAULT_ENV_FILE` and `TOKEN_ENV_KEY` are deleted, not bypassed;
  `TEMPLE_BRIDGE_ENV_FILE` is now a startup refusal naming both real doors.
  Before this, `--dump-config` on a clean environment reported
  `token: present (BRIDGE_TOKEN in ~/.config/sovereign-bridge.env)` — the shim's
  default was the master key.
- Configuring **both** transports, or **neither**, or a socket with no seat id, is
  a refusal. `--dump-config` reports the transport and *why*, by variable name,
  never a value, and exits 0 even when the answer is "none".
- The 3xx refusal now covers **both** transports.

**Doors — TIER TWO (law 8): the allowlist widens by exactly three read names.**

- `ALLOWED_BRIDGE_TOOLS` gains `arrive_lineage`, `current_policies`,
  `signals_summary`. Five POST targets, seven doors, one GET path. Still no write
  lane and still no pass-through.
- `stack_arrive` → `arrive_lineage`, `full_content: true`, `limit_per_bucket`
  clamped to 20, `source_instance` from `TEMPLE_SEAT_NAME` (a **bare model name**;
  unset or decorated is refused with the reason). On the seat transport the bridge
  OVERRIDES `source_instance` with the verified seat id, so the coverage line says
  so: an empty `to_self` bucket there is a routing fact, not an empty mailbox.
- `stack_policies` → `current_policies`, no filter, no `include_retired`.
- `stack_signals` → `signals_summary` with `mode` pinned to `summary` **outside**
  the schema. Renders `total`, `stale_24h`, `stale_7d`, `by_source`, `ingestion`
  and any `error`. A null count renders `unmeasured`, never `0`. Inner `ok: false`
  is a refusal; inner `ok: true` with an `error` is a **partial** read and renders
  counts *and* the error line.
- `stack_heartbeat` gains the `unacked_signals` total, on the same `unmeasured`
  rule.

**Result-type pin (law 3).** The bridge `json.loads` a tool's output and falls
back to the raw string, so `arrive_lineage` and `current_policies` arrive as
STRINGS. `TEXT_RESULT_TOOLS` / `JSON_RESULT_TOOLS` partition the allowlist, a test
asserts the partition is total and disjoint, and each helper refuses the other's
type — a string where an object belongs is the bridge's fail-open costume, an
object where text belongs is a changed tool shape.

**Version.** 0.3.1 → 0.4.0, so the `User-Agent` becomes `temple-stack/0.4.0`. That
string identifies harness traffic in bridge logs; a grep pinned to
`temple-stack/0.3.1` goes blind on this release.

### tests

- 38 → 98. Two fake bridges from one handler (TCP for the grant transport, a Unix
  socket for the seat transport), so the two paths are not two fixtures that can
  drift apart.
- **`test_conventions.py::TestCoverageAlwaysStated` no longer iterates a
  hand-written list of three doors** — that was a fail-open in the law-1 check
  itself: a new door needed no coverage line to stay green. It now drives off
  `TOOL_DEFINITIONS` and asserts its own table covers every door.
- **Deleted assertions, named because law 8 says a removed check is invisible to
  CI:** `test_env_file_parsing`, `test_missing_env_file_returns_empty_not_an_exception`,
  `test_env_override_wins_over_the_file` (test_shim.py) and
  `test_env_file_token_branch_reported_and_never_printed` (test_conventions.py).
  All four asserted that loading the master key out of a shell-sourceable file
  WORKED. They are replaced by `TestTransportResolution`, which asserts the
  parser and its constants no longer exist at all.

## 0.3.1 / 0.1.1 — unreleased (PR #1)

### mcp-shim 0.3.1

- Fail closed on HTTP 3xx. `urllib.request.urlopen` followed redirects and
  copied `Authorization` onto the next hop; anything answering on the bridge
  port could exfiltrate the token. `_http_json` now refuses redirects as a
  `BridgeError`. The 3xx body is closed before the refuse.

### log-distill 0.1.1

- `--list` paths and `DistillError` stderr now pass through `_redact`. A
  session directory named with a key-shaped string no longer leaves unmasked.
- Id-less tool/result matching no longer overwrites the first `callId=None`
  call with every subsequent id-less result.
- Redaction patterns gain `github_pat_`, Slack `xox[baprs]-`, and Google
  `AIza` shapes. Floor stated next to the pattern list: whitespace-splayed,
  base64-wrapped, homoglyphs, and bare UUID/hex house tokens are out of
  scope. `Bearer <20+>` is in scope. No hex catch-all (claim_ids would die).

### repo

- GitHub Actions workflow (`permissions: contents: read`; 3.10/3.11/3.12)
  calls `./run-tests.sh` so the suite has one definition.
