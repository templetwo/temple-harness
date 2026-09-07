# temple-stack MCP shim

A thin local **MCP (Model Context Protocol) stdio server** that wraps the Sovereign
Stack's local REST bridge, so any MCP-capable harness can give a local model
**read access to the chronicle**. Personalization through shared memory, not
changed weights.

Built for the DeepSeek Harness (`dsh`), whose MCP client spawns a command and
speaks JSON-RPC 2.0 over stdio. Nothing in it is dsh-specific — any MCP client
can drive it.

- **One file**, `temple_stack_mcp.py`, executable.
- **Stdlib only** (`urllib`, `http.client`, `socket`, `json`, `sys`, `os`). No pip,
  no venv, no wheels. It runs anywhere `python3` exists.
- Verified on Python 3.14.x (Studio) and 3.11; syntax floor 3.10 — CI runs 3.10,
  3.11 and 3.12.

## The read-only boundary, and why

Every tool this shim exposes is a READ, and there is **no pass-through tool** — a
caller cannot name a bridge tool, it can only pick one of the doors below.
The doors are listed, not counted: a count in prose rots the moment one is added,
and `tests/test_readme_claims.py` holds this table to the code.

| MCP tool | Bridge target | Method | Result |
|---|---|---|---|
| `stack_recall` | `recall_insights` | POST `/api/call` | object |
| `stack_latest` | `recall_insights` (same allowlist; order=newest, no query) | POST `/api/call` | object |
| `stack_open_threads` | `get_open_threads` | POST `/api/call` | object |
| `stack_arrive` | `arrive_lineage` | POST `/api/call` | text |
| `stack_policies` | `current_policies` | POST `/api/call` | text |
| `stack_signals` | `signals_summary` | POST `/api/call` | object |
| `stack_heartbeat` | `/api/heartbeat` | GET (no auth) | object |

Enforcement is a constant, not a runtime decision:

```python
ALLOWED_BRIDGE_TOOLS = frozenset({
    "recall_insights", "get_open_threads",
    "arrive_lineage", "current_policies", "signals_summary",
})
ALLOWED_BRIDGE_PATHS = frozenset({"/api/heartbeat"})
```

**The result column is part of the boundary, not documentation.** The bridge
`json.loads` a tool's output and falls back to the raw string, so `result` is an
object for tools that emit JSON and a string for tools that emit rendered prose.
`TEXT_RESULT_TOOLS` names the prose doors and `json_result_tools()` **derives**
the rest from `ALLOWED_BRIDGE_TOOLS` at call time; each helper refuses the
other's type — a string where an object belongs is the bridge's fail-open
costume, and an object where text belongs means the tool changed shape
underneath us.

**Why derived and not a second frozenset.** A second set would stand in front of
the first, and then widening `ALLOWED_BRIDGE_TOOLS` alone would change nothing —
so a reviewer performing law 3's negative control would mutate the boundary,
watch the suite stay green, and conclude the allowlist was not load-bearing.
`tests/test_canary.py` performs exactly that mutation. **One** constant widens
the POST lane. Measured by hand: with the derivation, widening the allowlist lets
the call reach the network (`BridgeError`); with a re-frozen second set the same
mutation still returns `BridgeToolNotAllowed`, which is the false green the
canary exists to catch.

`bridge_call()` raises `BridgeToolNotAllowed` for anything outside that set
**before it builds a request**, so a bug elsewhere in the file cannot widen the
scope by accident. Adding a write tool (`record_insight`, `handoff`,
`close_session`, …) requires editing those constants — the boundary is a diff,
and a diff gets reviewed.

**Why it matters:** the write lane stays with gated seats. Whether local models
ever get write scope is Anthony's ruling, and it is untaken. The seat socket now
makes a Studio-side write lane technically available — a seated terminal reaches
the stack's write tools with no credential at all — and the ruling is still his.
Nothing about that changes here: this shim is built so the ruling cannot be
pre-empted by a prompt, a jailbreak, or a careless argument, only by a code
change, and the code change has not been made.

## Three house lessons are baked in

1. **`order="relevance"` is pinned and non-negotiable.** The bridge's default is
   `newest`, which sorts hundreds of keyword-OR matches by timestamp and truncates
   — for any historical question you get recency noise with a reassuringly large
   `total_matched`. `stack_recall` sets `order="relevance"` unconditionally and
   **does not expose `order` in its input schema at all**, so a model cannot fall
   back into the noise.
2. **Coverage is always stated.** Every result restates the bridge's own
   partiality (`returned 2 of 214 matched | bridge-side truncated | more available
   from offset 2`). Silent partial reads are the failure this house has paid for
   most.
3. **Two truncations are kept distinguishable.** The bridge truncates by `limit`;
   this shim then truncates by character budget and says so separately, as
   `[truncated, 4000 of 9120 chars]`. A 9B model's context is precious, and a
   result that quietly drops half its content is worse than one that admits it.

## Failure behaviour (fails closed, speaks plainly)

- **Bridge unreachable / timing out / HTTP error** → a tool result with
  `isError: true` and text naming the problem, ending *"This is a failed call,
  not an empty result."* Never a crash, never an empty success.
- **Missing token at startup** → one line to stderr, exit code 1. The server does
  not start half-working.
- **The bridge's own fail-open shape** → the live bridge answers **HTTP 200 with
  `{"ok": true, "result": "Unknown tool: …"}`** for an unrecognised tool name.
  That is an error wearing a success costume. Both wrapped tools legitimately
  return objects, so the shim treats **any string-typed `result` as an error**
  rather than rendering it as chronicle content.
- **Unknown JSON-RPC method** → error `-32601`. **Malformed JSON** → `-32700`.
  **Notifications** get no response. The transport survives all of them.

## Inspecting the resolved configuration

`temple_stack_mcp.py --dump-config` prints exactly what would run — bridge
URL, timeout, caps, every door and its bridge target, both allowlists — and
exits without serving or touching the network. It works with **no token**
(inspection must not require credentials) and reports token **presence and
source only**; the value never prints, and a test fails if it ever does.

## The credential path — two transports, and never the master key

Anthony, 2026-09-05, firm: **inside the Studio, seats he seated use no tokens;
outside it, a seat asks for a scoped grant.** The master bridge key is not a seat
credential anywhere. Until 0.4.0 this shim's *default* was to read that key out
of `~/.config/sovereign-bridge.env`, so that path is gone and the variable that
named it is refused rather than ignored.

**(a) SEAT SOCKET — inside the Studio, no credential at all.**

```
SOVEREIGN_SEAT=hq-claude-studio        # set by ~/.sovereign/hq/seats/seat-*
TEMPLE_BRIDGE_SOCKET=<path>            # optional; default below
```

Selected by either variable being present. The shim speaks HTTP/1.1 over
`AF_UNIX` to `~/.sovereign/hq/seats/sock/bridge.sock`, sends
`X-Sovereign-Seat: $SOVEREIGN_SEAT`, and sends **no `Authorization` header at
all** — the bridge routes any Authorization header to the bearer check, so a
shim that sent both would silently stop being a seat.

**The seat id must be in the CALLING PROCESS's own environment.** The bridge
reads it through the kernel-attested peer pid, and macOS hides a system binary's
environment: launching through `/bin/bash -c` resolves to an ancestor instead.
Point the MCP client at a real `python3` with the variable in its env block.

**(b) SCOPED GRANT — everywhere else.**

```
TEMPLE_BRIDGE_TOKEN=<scoped grant>     # from the tap-to-arrive flow
TEMPLE_BRIDGE_URL=https://…            # default http://127.0.0.1:8100
```

**Neither, both, or the env file is a refusal, not a fallback.** Configuring both
transports is refused rather than ordered — a precedence rule would be a guess
wearing a policy costume. `--dump-config` reports which transport resolved and
*why*, by variable name, and never a value; it exits 0 even when the answer is
"none", because inspection is how an operator sees a broken configuration.

| Env var | Default | Purpose |
|---|---|---|
| `SOVEREIGN_SEAT` | *(unset)* | Seat id; presence selects the seat socket, value is the header |
| `TEMPLE_BRIDGE_SOCKET` | `~/.sovereign/hq/seats/sock/bridge.sock` | Seat socket path |
| `TEMPLE_BRIDGE_TOKEN` | *(unset)* | A **scoped grant**. Mutually exclusive with the seat transport |
| `TEMPLE_BRIDGE_URL` | `http://127.0.0.1:8100` | Bridge base URL (grant transport) |
| `TEMPLE_SEAT_NAME` | *(unset)* | The **bare model name** `stack_arrive` announces itself with |
| `TEMPLE_MCP_MAX_CHARS` | `4000` | Per-result character cap |
| `TEMPLE_MCP_TIMEOUT` | `20` | Bridge HTTP timeout, seconds |
| `TEMPLE_BRIDGE_ENV_FILE` | — | **REFUSED.** It named the master key's file |

Transport resolution is **lazy** (inside `main()` / `resolve_transport()`) and
**uncached**, so importing the module for testing never touches it and a running
server can never serve a stale answer.

### `TEMPLE_SEAT_NAME` is not `SOVEREIGN_SEAT`

A seat id is a registry entry (`hq-claude-studio`). A reader name is a model line
(`claude-fable-5`, `deepseek-r1-9b`). The lineage layer routes `to_self` letters
by **model line**, and its addressee filter matches the bare name only — a
decorated string hides that line's mail. `stack_arrive` refuses an unset or
whitespace-containing `TEMPLE_SEAT_NAME` and says which.

**On the seat transport the bridge OVERRIDES what this shim asks with.**
`seat_identity.sign_arguments` stamps the kernel-verified seat id over
`source_instance` for `arrive_lineage` — override, not setdefault — so on the
Studio the door filters `to_self` for a *seat id*, not for the model line. An
empty `to_self` bucket there is a routing fact, not an empty mailbox, and
`stack_arrive` prints that in its coverage line rather than leaving the reader to
misread an absence.

## Registering with an MCP client

Generic MCP stdio-server registration shape, which is what `dsh`'s MCP client
plugin (`@deepseek-ai/dsh-mcp-client`) consumes:

```json
{
  "mcpServers": {
    "temple-stack": {
      "command": "python3",
      "args": ["/absolute/path/to/temple-harness/mcp-shim/temple_stack_mcp.py"],
      "env": {
        "TEMPLE_BRIDGE_TOKEN": "<scoped grant>",
        "TEMPLE_SEAT_NAME": "deepseek-r1-9b"
      }
    }
  }
}
```

On the Studio, use the seat socket and no credential at all:

```json
{
  "mcpServers": {
    "temple-stack": {
      "command": "/opt/homebrew/bin/python3",
      "args": ["/absolute/path/to/temple-harness/mcp-shim/temple_stack_mcp.py"],
      "env": {
        "SOVEREIGN_SEAT": "hq-claude-studio",
        "TEMPLE_SEAT_NAME": "claude-fable-5",
        "TEMPLE_MCP_MAX_CHARS": "2500"
      }
    }
  }
}
```

> **The dsh-side stanza is TO BE CONFIRMED on the MacBook.** `dsh` is installed at
> `~/dsh-eval` on the MacBook, not on this machine, so its exact config file
> location and key names could not be verified here. The block above is the
> conventional MCP-server registration shape, not a verified dsh schema — check it
> against the installed dsh docs before trusting it. What *is* verified is the
> server side: the shim speaks standard MCP stdio and completes a full
> initialize → tools/list → tools/call cycle.
>
> The bridge is **localhost-only**, so the harness must run on a machine that can
> reach `127.0.0.1:8100` — or `TEMPLE_BRIDGE_URL` must point at a reachable
> Stack endpoint.

## Protocol notes

- **Framing: newline-delimited JSON-RPC 2.0**, one compact JSON object per line
  on stdout. This is the MCP stdio transport; `Content-Length` framing is LSP, not
  MCP. All diagnostics go to **stderr only** — anything on stdout corrupts the
  stream.
- **Version negotiation:** the shim accepts what the client offers when it is one
  it speaks (`2025-06-18`, `2025-03-26`, `2024-11-05`) and echoes it back;
  otherwise it answers with `2025-06-18`.
- **Batching** was removed in MCP 2025-06-18, so a top-level JSON array is
  rejected with `-32600` rather than silently half-processed.

## Running the tests

```bash
cd mcp-shim
python3 -m unittest discover -s tests -v
```

Stdlib `unittest`, no external deps. They stand up **two fake bridges** — one on
a random localhost port for the grant transport, one on a Unix socket in a temp
dir for the seat transport — from the **same handler**, so the two paths are
compared against one server behaviour rather than two fixtures that could drift.
They need **neither the real bridge nor any real credential**, and they never read
the master key's env file: that file is refused now, and the refusal has tests.

Coverage includes: the initialize handshake and version negotiation; `tools/list`
returning every door in the table above with the right schemas (and *no* `order`
field); the seat socket sending its header and **no** `Authorization`; both
transports refusing a 3xx; transport refusal for none / both / env-file; the
bidirectional result-type pin; the new doors' forwarded arguments and coverage
lines; `unmeasured` never rendering as `0`;
`order=relevance` present in the forwarded recall body even when the caller tries
to override it; limit clamping; truncation firing with its marker; the allowlist
refusing write tools (tested on the internal function directly, and over the
wire); a bridge-unreachable call failing closed; the HTTP-200 `Unknown tool:`
fail-open being caught; and the no-token startup exit.

Each of these gates was **verified able to fail**: mutating the source to break
the pinned order, widen the allowlist, disable truncation, or drop the fail-open
check each turns the suite red, and the pristine file turns it green again.


---

## v0.2.0 — stack_latest, the sanctioned recency door (2026-08-24)

A fourth read-only tool, proposed by the MacBook seat from live use (a 9B model asked
"recall the most recent" and had no door), reviewed and landed by HQ. `stack_latest`
is a query-less tail read: `order=newest` pinned outside the schema, no search terms
anywhere, and a server-side groove-guard that refuses any `query` with an explicit
redirect to `stack_recall`. The recency-noise trap the relevance pin exists for is
newest-ordered *search*; with no query there is no match set, so the trap cannot
re-enter. Zero new bridge surface: it reuses the allowlisted `recall_insights`
target, and a test asserts the allowlist is still exactly two POST names + one GET path.

## dsh registration — CONFIRMED stanza (MacBook, 2026-08-24)

dsh has **no `mcpServers` JSON**. Registration is a cordis patch row in
`$DSH_HOME/profiles/<name>/cordis.patch.yml`, and a NEW entry must be wrapped in the
patch grammar's `insert:` key (a bare row is treated as an id-targeted override and
rejected). Working stanza, verified by `--dump-config` and live run:

```yaml
- insert:
    - id: mcp-temple-stack
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: temple-stack
        transport: stdio
        command: /opt/homebrew/bin/python3
        args: ['<path-to>/temple-harness/mcp-shim/temple_stack_mcp.py']
        env:
          TEMPLE_BRIDGE_URL: https://stack.templetwo.com   # any seat that is not HQ; the 127.0.0.1:8100 default exists only on the Studio
          TEMPLE_MCP_MAX_CHARS: '2500'
```

Python note: verified on 3.14.x (both seats); syntax floor 3.10; pin a real python3
(the MacBook's PATH python3 is a conda 3.10 — the shim compiles there but the
verified runs are 3.14).


---

## v0.4.0 — the substrate carries more (2026-09-06)

Three read-only doors and a credential path that never carries the master key.
Built against the Stack as deployed 2026-09-06 (52 published tools, 48 retired;
none of this shim's doors were among them).

**Doors added.** `stack_arrive` (`arrive_lineage`, full content, bounded
`limit_per_bucket`, reader from `TEMPLE_SEAT_NAME`), `stack_policies`
(`current_policies`, no filter), `stack_signals` (`signals_summary`, `mode` pinned
to `summary` outside the schema). All reads. The write lane is still none.

**`stack_signals` renders `unmeasured`, never `0`.** The stack nulls a count it
could not honestly answer, and printing a zero there would reproduce this house's
own fail-open one layer out. `ok: false` inside the envelope is a refusal (an
error result); `ok: true` with a non-null `error` is a **partial** read and renders
the counts *and* an error line. The door reads exactly `ok`, `error`, `ingestion`,
`total`, `stale_24h`, `stale_7d`, `by_source` — it does not read
`total_configured`, `corrupt_rows`, `source_status` or `sources_degraded`, so it
can never publish a narrower number under a wider name.

**Reachability, stated because it differs by transport.** On the seat socket all
five allowlisted targets are reachable. On a scoped `read` grant,
`signals_summary` is **not** mapped (`session_tokens.TOOL_SCOPES`), so
`stack_signals` gets a legible 403 until HQ's separate bridge change lands. The
other four are mapped today.

**Version.** `SERVER_VERSION` 0.3.1 → 0.4.0, which changes the `User-Agent` to
`temple-stack/0.4.0`. That string is how harness traffic is identified in bridge
logs; a log-side grep pinned to `temple-stack/0.3.1` goes blind on this release.
