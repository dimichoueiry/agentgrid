# AgentGrid Teams (agent pipelines) — PRD

**Status:** Phase 1 (engine) built and proven; Phase 2 (routes + board UI) to do
**Date:** 2026-08-13
**Owner:** Dimitri El-Choueiry
**Related:** reuses `agentgrid/chat.py` (the headless-CLI engine on `main`)

---

## 1. Summary

An authoring layer that composes real Claude Code sessions into a **pipeline**:
one node's output feeds the next node's input, with **loops** ("send the draft
back to the writer until the reviewer approves"). Every node is a real `claude
-p` turn on the **user's own login** — no SDK, no API key, no LangGraph. Claude
Code already *is* the agent; Teams only decides who runs, in what order, and
what each is told.

## 2. Why this shape (not a framework)

Frameworks (LangGraph/CrewAI/…) make their own model calls → API key, per-token
billing, a second agent runtime. That breaks AgentGrid's whole premise. Teams
instead drives the same `chat.ChatSession` the chat panel uses, so a pipeline is
just several real sessions on your subscription, visible on the board.

## 3. The team format (data — a team is authored, not coded)

A team is a JSON object (see `examples/teams/writing-team.json`):

```jsonc
{
  "name": "writing-team",
  "cwd": "/abs/path/to/repo",          // where the agents run
  "nodes": [
    { "id": "research", "role": "researcher", "posture": "read-only",
      "prompt": "Research the topic.\n\n{input}" },
    { "id": "write", "role": "writer", "posture": "auto",
      "prompt": "Write it, using:\n{research}\n\nAddress feedback:\n{review}" },
    { "id": "review", "role": "reviewer", "posture": "read-only",
      "prompt": "Reply APPROVED, or NEEDS_WORK + fixes.\n\n{write}" }
  ],
  "loops": [
    { "at": "review", "back_to": "write", "when": "NEEDS_WORK", "max": 3 }
  ]
}
```

- **Wiring is templating.** `{input}` is the team input; `{node_id}` is that
  node's latest output. Unknown `{braces}` are left untouched. Every node id
  starts empty, so `{review}` before the reviewer runs renders blank.
- **Nodes are sessions.** Each node owns one persistent `ChatSession`. A loop
  back to a node **resumes** it, so the writer keeps its draft and *revises*
  rather than restarting. `posture` is `auto` or `read-only` (from `chat.py`).
- **Loops are explicit and bounded.** A loop watches `at`'s output for `when`
  (case-insensitive substring); while present and under `max`, it jumps to
  `back_to` with a feedback message (`{at}` = the tested node's output).

## 4. The engine (Phase 1 — done)

`agentgrid/teams.py`:
- `load_team(dict) -> Team` (validates; raises `ValueError` with the reason),
  `save_team` / `list_teams` (stored as JSON under `~/.agentgrid/teams/`).
- `render(template, values)` — the `{placeholder}` substitution.
- `TeamRun` — runs nodes in order, wired by templating, with loop jumps and a
  one-shot feedback prompt on loop-back. Streams events (below). `cancel()`
  stops the run and its current node.
- `TeamManager` — one running execution per team name.

**Verified:** 10 unit tests (templating, validation, the review loop firing +
being capped + feedback reaching the writer) with stubbed sessions, plus a live
run of the writing team against the real CLI 2.1.x on the subscription login.

## 5. The event contract (SSE) — handoff boundary for the board UI

`TeamRun` emits, in the chat panel's own vocabulary where it can:

```jsonc
{ "type":"team_started", "name":"…", "nodes":[{"id","role"}] }
{ "type":"node_started",  "id":"write", "role":"writer" }
{ "type":"node_event",    "id":"write", "event": <a chat.py event> }  // live activity
{ "type":"node_done",     "id":"write", "output":"…", "ok":true }
{ "type":"loop",          "at":"review", "backTo":"write", "iter":1, "max":3 }
{ "type":"team_done",     "ok":true, "cancelled":false, "outputs":{…} }
```

`node_event.event` is exactly a chat.py event (`assistant_message`, `tool_use`,
`tool_result`, `thinking`, `turn_started`, `turn_done`, `error`), so the board
reuses the chat renderer per node.

## 6. Phase 2 — routes + board UI (next)

**Backend routes** (token-gated, loopback, like the rest):
- `GET  /api/teams` → list saved team defs
- `POST /api/teams` → save a team def (this is the *builder's* write path)
- `POST /api/teams/run` `{name, input}` → start a run
- `GET  /api/teams/stream?name=…` → SSE of the run's events (§5)
- `POST /api/teams/cancel` `{name}`
- `GET  /api/teams/state?name=…` → `{running, currentNode, iter}`

**Board UI** (for a UI specialist, against §5):
- A **Teams** view: list of teams + a run button + an input box.
- A **builder**: add nodes (role + prompt + posture), wire output→input by
  referencing `{node_id}` in prompts, add a loop (at → back_to, when, max).
- A **live pipeline/graph**: nodes as a chain, the active node lit, the loop
  edge animating on each iteration; click a node to see its live chat events.

## 7. Out of scope (for now)

General DAGs (parallel branches / fan-out), nested teams, and per-node human
approval. v1 is a linear pipeline with loops — which already expresses the
research → write → review → revise pattern people actually want.
