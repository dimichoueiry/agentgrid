# AgentGrid Chat Panel — PRD

**Status:** Draft, for review
**Date:** 2026-08-07
**Owner:** Dimitri El-Choueiry
**Related:** builds on the existing web board (`agentgrid/web.py`, `agentgrid/static/app.html`) and session model (`agentgrid/discovery.py`)

---

## 1. Summary

Add a chat panel to the AgentGrid board that lets you talk to a Claude Code
session in the browser — rendered as clean chat, not a terminal — while it runs
as **real, headless Claude Code** on your machine, in your repo, with all its
tools. It drives `claude -p --output-format stream-json` under the hood and
renders the streamed events.

## 2. Problem

The terminal TUI is functional but visually rough, and the only way to interact
with a session today is to open its terminal tab. We want a nicer, native
interaction surface **without** giving up any Claude Code capability.

## 3. Goals / Non-goals

**Goals**
- Chat with a session from the board; responses render as markdown + tool cards.
- It is the *real* Claude Code: same tools, same local process, same repo, same
  config. Zero capability loss.
- Clean handoff: backend and frontend developed against a fixed event contract,
  so a UI specialist can build the panel independently.

**Non-goals (v1)**
- Not replacing the terminal, IDE extension, or desktop app — they all keep working.
- Not a full IDE (no file tree, no editor).
- Not remote/hosted — stays loopback + token-gated + local, like the rest of the app.
- Not multi-user.

## 4. Principle: it is the real Claude Code

`claude -p` (print/headless) is the **same binary** as the interactive CLI. `-p`
changes the *output* (a JSON event stream instead of a drawn TUI), not the
*engine*. Concretely, a chat turn:

- runs `claude` as a local subprocess with the working directory set to the
  session's repo — it edits your actual files and runs your actual commands;
- loads your `CLAUDE.md`, memory, skills, MCP servers, permissions, and auth
  (no `--bare`);
- with `--resume <session_id>`, continues the *same* conversation and transcript
  the board already tracks.

Nothing is sandboxed or stripped. The panel is a nicer face on the same engine.

## 5. Users & primary flow

1. On the board, click a session → a **Chat** panel opens on the right.
2. Type a message, press Enter.
3. Watch it stream: assistant text as bubbles, tool calls as cards ("Edited
   `web.py`", "Ran `pytest` → 72 passed"), diffs inline.
4. The work happens in the real repo, live. Stop button cancels the turn.

## 6. Architecture

### 6.1 Backend components (my scope — build after PRD approval)

1. **`agentgrid/chat.py` — turn manager.**
   - `start_turn(session_id, message, cwd, permission_mode) -> turn_id`: spawn
     `claude -p --resume <sid> --output-format stream-json --verbose
     --include-partial-messages [--permission-mode <mode>] "<message>"` with
     `cwd=<repo>`; stream stdout.
   - In-memory registry `turn_id -> {process, queue}` so turns can be streamed
     and cancelled. No files written (consistent with the poller's discipline).
   - `events(turn_id)`: generator yielding **normalized** UI events (see 6.2),
     parsed from stdout lines, deferring partial trailing lines (same technique
     as `TranscriptCache`).
   - `cancel(turn_id)`: terminate the subprocess and its group; clean up.

2. **HTTP routes in `web.py`** (token-gated, loopback, like every route):
   - `POST /api/chat` `{sessionId, message}` → validates, resolves the session's
     cwd from the fleet, starts a turn, returns `{turnId}`.
   - `GET /api/chat/stream?turn=<id>` → **Server-Sent Events**: writes `data:
     <json>\n\n` per normalized event until `turn_done`. `ThreadingHTTPServer`
     already serves each request on its own thread, so a held-open stream is
     fine. On client disconnect → cancel the turn (kill the subprocess).
   - `POST /api/chat/cancel` `{turn}` → cancel.

3. **Session resolution & guards.** cwd comes from the tracked session (never
   client-supplied); refuse to chat a session whose status is `working`
   (resuming a live turn would fork/conflict — see Open Decisions).

### 6.2 The contract — SSE event API (the handoff boundary)

This is the fixed interface between backend and frontend. The UI specialist codes
against exactly this; the backend guarantees exactly this. Each SSE `data:` line
is one JSON object:

```jsonc
{ "type": "turn_started",   "turnId": "t_ab12", "sessionId": "…" }
{ "type": "assistant_delta","text": "partial streaming text…" }
{ "type": "assistant_message","text": "a complete assistant message (markdown)" }
{ "type": "tool_use",       "id": "tu_1", "name": "Edit", "input": { … } }
{ "type": "tool_result",    "id": "tu_1", "ok": true, "summary": "web.py +12 −3" }
{ "type": "turn_done",      "turnId": "t_ab12", "stats": { "durationMs": 8400 } }
{ "type": "error",          "message": "human-readable reason" }
```

Design rule: the backend **normalizes** Claude Code's raw `stream-json` into this
small vocabulary, so the UI never has to know the CLI's internal event shapes and
a CLI change can't break the panel.

### 6.3 Frontend components (UI specialist's scope)

Built against §6.2 only. Reuses existing helpers where possible.

1. **Chat panel** — slide-in container per session (mirror the existing detail
   panel pattern).
2. **Message list** — user bubbles; assistant bubbles rendered via the existing
   `md()` markdown-lite function; **tool-call cards** for `tool_use`/`tool_result`.
3. **Composer** — textarea + Send + Stop.
4. **Streaming render** — consume the SSE stream, append `assistant_delta` text
   live, finalize on `assistant_message`.
5. **States** — idle / streaming / error / empty, matching the board's visual language.

## 7. Permissions (the key decision)

Interactive Claude Code asks "run this? (y/n)". A non-terminal UI must handle
that. Two paths:

- **A. Bounded permission mode (recommended for v1).** Run with a mode such as
  `acceptEdits` so edits/commands within the session's repo proceed, and render
  every tool action clearly + a Stop button. Simple; ships fast. Risk: it
  auto-runs tools without a per-action prompt.
- **B. Approval UI (later phase).** Backend surfaces a permission request as an
  event; the panel shows Allow/Deny; the decision is sent back. Needs Claude
  Code's headless permission-prompt mechanism (exact hook — a permission-prompt
  MCP tool vs. the Agent SDK `canUseTool` callback — **to be verified before
  building B**). More work, safest UX.

## 8. Phases & estimates (estimates are for me, the backend builder)

- **P1 — prove it streams (~half a day).** `chat.py` + SSE route + subprocess,
  passing raw-normalized events. Test via `curl` / console: send a message to a
  real session, watch tool calls edit a real file. Go/no-go on the whole idea.
- **P2 — backend complete (~2–3 days).** Full §6.2 normalization, cancel +
  disconnect cleanup, cwd resolution, `working`-session guard, permission mode.
  UI specialist builds §6.3 in parallel against the contract.
- **P3 — polish (ongoing).** Approval UI (option B), inline diffs, richer tool
  cards, new-session-from-panel, error/empty states.

## 9. Open decisions (need your call before I build the backend)

1. **Permission model for v1** — A (bounded auto-accept, recommended) or B
   (approval UI now)?
2. **Which sessions are chattable** — recommend: idle / replied / done sessions
   only; block while `working`; and because resuming an interactive session
   forks it, offer "start a new session here" instead of resuming those.
3. **New sessions from the panel** — allow starting a brand-new session (no
   `--resume`, just `claude -p` in a chosen repo) in v1, or resume-only first?

## 10. Risks & mitigations

- **Auto-accept does something unwanted** → bounded mode + every action visible +
  Stop; scope to the session's own repo.
- **Resuming a live session forks/conflicts** → block chat while `working`.
- **SSE + threaded server edge cases** (client disconnect, zombie process) → kill
  the subprocess on disconnect; reap on `turn_done`.
- **Resume continuity across session kinds** (background vs interactive
  transcripts) → verify `--resume` behavior in P1.

## 11. Success criteria

From the board, send a message to a real session and watch it — in the browser,
with no terminal — stream a response and **edit a real file in the real repo**.
The transcript stays continuous with what the board already shows.

## 12. Out of scope / future

Hosted/remote use, multi-user, file tree/editor, and the approval UI (option B)
are explicitly later. v1 is: chat one session, one repo, streaming, real tools.
