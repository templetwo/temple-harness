# temple-stack MCP shim

A thin local **MCP (Model Context Protocol) stdio server** that wraps the Sovereign
Stack's local REST bridge, so any MCP-capable harness can give a local model
**read access to the chronicle**. Personalization through shared memory, not
changed weights.

Built for the DeepSeek Harness (`dsh`), whose MCP client spawns a command and
speaks JSON-RPC 2.0 over stdio. Nothing in it is dsh-specific — any MCP client
can drive it.

- **One file**, `temple_stack_mcp.py`, executable.
- **Stdlib only** (`urllib`, `json`, `sys`, `os`). No pip, no venv, no wheels.
  It runs anywhere `python3` exists.
- Verified on Python 3.14.5 (the Mac Studio's Homebrew `python3`); written
  against 3.12+ syntax.

## The read-only boundary, and why

The shim exposes **exactly three tools, all reads**, and there is **no
pass-through tool** — a caller cannot name a bridge tool, it can only pick one of
three doors that were opened for it.

| MCP tool | Bridge target | Method |
|---|---|---|
| `stack_recall` | `recall_insights` | POST `/api/call` |
| `stack_open_threads` | `get_open_threads` | POST `/api/call` |
| `stack_heartbeat` | `/api/heartbeat` | GET (no auth) |

Enforcement is a constant, not a runtime decision:

```python
ALLOWED_BRIDGE_TOOLS = frozenset({"recall_insights", "get_open_threads"})
ALLOWED_BRIDGE_PATHS = frozenset({"/api/heartbeat"})
```

`bridge_call()` raises `BridgeToolNotAllowed` for anything outside that set
**before it builds a request**, so a bug elsewhere in the file cannot widen the
scope by accident. Adding a write tool (`record_insight`, `handoff`,
`close_session`, …) requires editing those constants — the boundary is a diff,
and a diff gets reviewed.

**Why it matters:** the write lane stays with gated seats. Whether local models
ever get write scope is Anthony's ruling, and it is untaken. This shim is built
so that ruling cannot be pre-empted by a prompt, a jailbreak, or a careless
argument — only by a code change.

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

## Configuration

All optional except the token.

| Env var | Default | Purpose |
|---|---|---|
| `TEMPLE_BRIDGE_URL` | `http://127.0.0.1:8100` | Bridge base URL |
| `TEMPLE_BRIDGE_TOKEN` | *(unset)* | Token override; skips the env file. Used by tests |
| `TEMPLE_BRIDGE_ENV_FILE` | `~/.config/sovereign-bridge.env` | Shell-sourceable env file to parse |
| `TEMPLE_MCP_MAX_CHARS` | `4000` | Per-result character cap |
| `TEMPLE_MCP_TIMEOUT` | `20` | Bridge HTTP timeout, seconds |

The production path parses the bridge env file for its bearer credential. The
value is read into memory only — it is never printed, never logged, and never
placed in a URL. Token resolution is **lazy** (inside `main()`), so importing the
module for testing never touches it.

## Registering with an MCP client

Generic MCP stdio-server registration shape, which is what `dsh`'s MCP client
plugin (`@deepseek-ai/dsh-mcp-client`) consumes:

```json
{
  "mcpServers": {
    "temple-stack": {
      "command": "python3",
      "args": ["/absolute/path/to/temple-harness/mcp-shim/temple_stack_mcp.py"]
    }
  }
}
```

Optionally pin non-default settings without touching the code:

```json
{
  "mcpServers": {
    "temple-stack": {
      "command": "python3",
      "args": ["/absolute/path/to/temple-harness/mcp-shim/temple_stack_mcp.py"],
      "env": {
        "TEMPLE_MCP_MAX_CHARS": "2500",
        "TEMPLE_BRIDGE_URL": "http://127.0.0.1:8100"
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

38 tests, stdlib `unittest`, no external deps. They stand up a **fake bridge** on
a random localhost port and point the shim at it via `TEMPLE_BRIDGE_URL`, so they
need **neither the real bridge nor the real token**.

Coverage includes: the initialize handshake and version negotiation; `tools/list`
returning exactly three tools with the right schemas (and *no* `order` field);
`order=relevance` present in the forwarded recall body even when the caller tries
to override it; limit clamping; truncation firing with its marker; the allowlist
refusing write tools (tested on the internal function directly, and over the
wire); a bridge-unreachable call failing closed; the HTTP-200 `Unknown tool:`
fail-open being caught; and the no-token startup exit.

Each of these gates was **verified able to fail**: mutating the source to break
the pinned order, widen the allowlist, disable truncation, or drop the fail-open
check each turns the suite red, and the pristine file turns it green again.
