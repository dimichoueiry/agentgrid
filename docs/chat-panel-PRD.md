# AgentGrid Chat Panel — PRD (v2, decisions locked)

**Status:** For review
**Date:** 2026-08-07
**Owner:** Dimitri El-Choueiry
**Related:** extends the web board (`agentgrid/web.py`, `agentgrid/static/app.html`) and session model (`agentgrid/discovery.py`)

---

## 1. Summary

Chat with a Claude Code session **in the browser**, rendered as clean chat
(not a terminal), while it runs as the **real `claude` CLI** on your machine —
your repo, your tools, **your existing Claude Code login**. Under the hood it
drives `claude -p --output-format stream-json` and renders the streamed events.

## 2. Problem

The terminal TUI is visually rough and the only way to interact with a session
is its terminal tab. We want a nicer interaction surface with **zero capability
loss and no change to how sessions run or bill**.

## 3. Decisions locked (from review)

- **Engine: the CLI, not the Agent SDK.** The SDK requires a *paid API key* and
  is not allowed to use the Claude Code subscription login (Anthropic policy).
  The CLI uses each user's **existing login** — essential for you *and* for
  anyone you distribute to (they sign in once with their own Claude Code, no
  keys, no per-token bill).
- **Libraries: Python stdlib only.** No Agent SDK, **no LangGraph / LangChain**,
  no agent framework. See §6.1.
- **Permissions v1:** a **mode selector** in the UI (auto-accept / default /
  plan). Per-tool Allow/Deny is Phase 2 (needs a small MCP approval server).
- **Steering v1:** a **queue** — the next message sends when the current
  response finishes. Mid-response injection is out of scope (SDK-only).
- **New sessions from the panel:** yes.

## 4. Principle: it is the real Claude Code, on your login

`claude -p` (headless/print) is the **same binary** as the interactive CLI. It
runs as a local subprocess with `cwd` set to the session's repo — same tools,
same files, same config, same auth. The `system/init` event even reports
`apiKeySource`, so we can *show* that it's your subscription, not an API key.
Nothing is sandboxed or stripped.

## 5. Users & primary flow

1. Click a session on the board → a **Chat** panel opens on the right.
2. Type a message, press Enter.
3. Watch it stream: assistant text as bubbles, tool calls as cards ("Edited
   `web.py`", "Ran `pytest` → 72 passed"). Real edits in the real repo. Stop
   cancels the turn.
4. Type again while it's working → queued, sent when the current response ends.

## 6. Architecture

### 6.1 Libraries / dependencies (explicit)

**Stdlib only. Zero new dependencies.**
- `subprocess` — drive `claude -p` (stdin = `/dev/null`, message passed as an arg).
- `http.server` — SSE stream (already the app's server).
- `json` — parse the `stream-json` output.
- `threading` — already used by the fleet poller.

**Not using — and why:** the Agent SDK (breaks the login model, §3), and
**LangGraph / LangChain / any agent framework**. Those build an agent loop; but
**Claude Code already *is* the agent** — it has its own loop, tools, memory, and
permissions. Wrapping it in a second framework is the wrong layer: heavier, more
to break, and it buys nothing here. We drive one agent and render its output.

### 6.2 Backend components (my scope)

1. **`agentgrid/chat.py` — turn/queue manager.**
   - `send(session_id, message, permission_mode)`: enqueue a message for a
     session. If idle, start a turn now; if a turn is running, it fires when
     that turn finishes (the v1 "queue").
   - A turn = `claude -p --resume <sid> --output-format stream-json --verbose
     [--permission-mode <mode>] "<message>"`, `cwd=<repo>`, `stdin=DEVNULL`.
   - Parse stdout JSONL incrementally (defer partial trailing lines, same
     discipline as `TranscriptCache`) → **normalize** to §6.3 events.
   - In-memory registry `session_id -> {process, queue, subscribers}` (no files
     written on the hot path, matching the poller's rule).
   - `cancel(session_id)`: terminate the process group; clear the queue.

2. **HTTP routes in `web.py`** (token-gated, loopback, like every route):
   - `POST /api/chat` `{sessionId, message, permissionMode?}` → resolve the
     session's cwd from the fleet, enqueue/start, return `{ok}`.
   - `GET /api/chat/stream?session=<id>` → **SSE**: `data: <json>\n\n` per
     normalized event. `ThreadingHTTPServer` serves each on its own thread; on
     client disconnect → cancel the turn.
   - `POST /api/chat/cancel` `{sessionId}` → stop + clear queue.
   - `GET /api/chat/state?session=<id>` → `{running, queued}` for the UI.

3. **Session resolution & guards.** cwd comes from the tracked session (never
   client-supplied). New session = same flow without `--resume`; capture the new
   `session_id` from the `init`/`result` event so the board picks it up.

### 6.3 The event contract — SSE API (handoff boundary; verified vs CLI 2.1.201)

The backend maps the CLI's raw `stream-json` to this small vocabulary. The UI
codes against *only* this; a CLI change can't break the panel. One JSON object
per SSE `data:` line:

```jsonc
// from CLI system/init
{ "type":"turn_started",   "sessionId":"…", "model":"…", "cwd":"…", "authSource":"subscription" }
// from an assistant text block
{ "type":"assistant_message","text":"markdown text" }
// from an assistant thinking block (UI may mute/hide)
{ "type":"thinking",       "text":"…" }
// from an assistant tool_use block
{ "type":"tool_use",       "id":"toolu_…", "name":"Edit", "input":{…} }
// from the user/tool_result message
{ "type":"tool_result",    "id":"toolu_…", "ok":true, "summary":"note.txt read" }
// from CLI result
{ "type":"turn_done",      "ok":true, "result":"…", "stats":{ "durationMs":8400, "numTurns":2, "costUsd":0.32 } }
{ "type":"error",          "message":"…" }        // api/rate-limit/spawn errors
```

*(v1 streams whole assistant blocks. Token-by-token deltas via
`--include-partial-messages` are a P3 nicety, not required.)*

### 6.4 Frontend components (UI specialist's scope)

Built against §6.3 only. Reuse existing helpers: chat panel (mirror the detail
panel), message bubbles (assistant via the existing `md()`), tool-call cards,
composer (Send + Stop), streaming render, idle/streaming/error/empty states.

## 7. Permissions (decided)

- **v1 — mode selector** in the panel, applied per turn via `--permission-mode`:
  `bypassPermissions`/`acceptEdits` (auto-accept), `default`, `plan`. Every tool
  action is rendered + a Stop button; scope is the session's own repo.
- **Phase 2 — per-tool Allow/Deny** via `--permission-prompt-tool`: the daemon
  runs a tiny MCP "approval" tool that blocks, forwards the request to the
  browser over SSE, and returns the decision. Flagged: the permission tool's I/O
  schema is undocumented (reverse-engineered) and it can't approve MCP tools
  marked `requiresUserInteraction`.

## 8. Steering / queue (decided)

- **v1:** queue; the next message fires when the current response finishes (one
  `claude -p` per turn — natural chat ordering).
- **Not in v1:** mid-response injection between tool calls (SDK streaming-input
  only, which needs the paid API key).

## 9. Phases & estimates (for me, the backend builder)

- **P1 — prove it streams (~half a day).** `chat.py` + SSE route + subprocess +
  §6.3 normalization for one session. `curl`-tested: send a message to a real
  session, watch tool calls edit a real file. Go/no-go.
- **P2 — backend complete (~2 days).** Queue, cancel + disconnect cleanup,
  permission-mode selector, new-session, `working`-session guard, tests. UI
  specialist builds §6.4 in parallel against the contract.
- **P3 — polish (ongoing).** Per-tool approval (§7 Phase 2), token-delta
  streaming, richer tool cards, cost display.

## 10. Risks & mitigations

- **Auto-accept does something unwanted** → every action visible + Stop; scope
  to the session's repo; default the selector to a non-bypass mode.
- **Resuming a `working` session forks/conflicts** → guard (queue to it instead
  of starting a competing turn; block if truly mid-turn elsewhere).
- **SSE + threaded server edge cases** (disconnect, zombie process) → kill the
  subprocess on disconnect; reap on `turn_done`.
- **Cost visibility** → surface `total_cost_usd` from the `result` event so a
  turn's cost is never hidden.

## 11. Success criteria

From the board, send a message to a real session and watch it — in the browser,
no terminal — stream a response and **edit a real file in the real repo**, on
**your** login, transcript continuous with what the board shows.

## 12. Out of scope

SDK/API-key-only features (mid-response steering, clean per-tool approval as a
callback), hosted/remote/multi-user. v1 is: chat one session, one repo,
streaming, real tools, your login.
