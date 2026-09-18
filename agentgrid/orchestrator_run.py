"""Running an orchestrator: a bounded tool-calling loop over an OpenRouter model.

The loop is four lines long in principle -- ask the model what to do, do the
parts it is allowed to do, write down what happened, ask again -- and the rest
of this module is the "allowed to do" part, which is where all the care is.

What keeps it honest:

- **Every tool is executed here.** The model receives a menu, not a shell, and
  a call coming back is dispatched to a method on `Run` or it fails. Starting
  an agent goes through the `Host`, which is the same spawn boundary the New
  agent sheet uses: a directory that is not one of the discovered projects is
  refused there, exactly as it is for a human.
- **The harness owns the limits.** Steps, spawns, concurrent children and the
  orchestrator's own OpenRouter spend are counted here and enforced before the
  call happens. Instructions to the model are courtesy; these are the fence.
- **Approvals suspend the run rather than block a thread.** In `ask` mode a
  request to start an agent is written into the state as `pending`, the status
  becomes `waiting` (the board's "Needs you"), and the worker thread exits.
  Approving or declining resumes from disk -- so an approval can outlive a
  server restart, and nothing is held open waiting for a human.
- **AgentGrid does the waiting.** While an orchestrator's agents work, the
  harness watches the board for it -- free, no model calls -- and wakes it
  only when one of them finishes, blocks or leaves, with the tail of that
  agent's output attached. A model that ends its turn with a progress note
  while its agents work is watching, not waiting on the user; only a run
  with nothing in flight hands the turn back to a person.
- **The state is written after every step.** A run interrupted mid-step (the
  lid closed) resumes from the last completed step: the unfinished model turn
  is dropped and the model is told to check what already exists before it
  starts anything, because a spawn is a side effect that must never be
  silently repeated.

What the model is offered and told lives next door, in
`orchestrator_brief`: the menu and the standing prompt are wording, and this
module is behaviour. Both are rebuilt from the persona at every step, so an
edit in the persona bank -- or a lesson just remembered -- applies to a run
already in flight.

The persona's agent defaults and allowed models are enforced here too: a
start_agent call is filled in from the defaults *before* it is gated, so the
approval banner shows what will really run, and a model outside the fence is
refused before anyone is asked to approve it.

The `Host` is the only way out of this module to the machine. That keeps the
engine testable with a fake host, and it is what a non-macOS host will differ
in: it simply reports that interactive sessions are unavailable, and the tool
menu offered to the model shrinks to match.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time

from agentgrid import areas, openrouter, orchestrator, personas, tickets
from agentgrid.orchestrator_brief import MAX_WAIT_SECONDS, Context, system_prompt, tool_specs

# A tool result is context for the next decision, not an archive: a 200k-line
# transcript summarised into 20k characters still says what happened.
MAX_TOOL_RESULT = 20_000
# How much conversation a request may carry. The system prompt and the goal are
# always kept; the middle is dropped before the tail.
CONTEXT_BUDGET = 120_000
WAIT_CHUNK_SECONDS = 2.0
# How often the watcher looks at the board. The fleet itself refreshes about
# every two seconds, so looking faster only re-reads the same answer.
WATCH_POLL_SECONDS = 3.0
# A child is "starting" for this long after launch, before the fleet has seen
# it -- so a run that has just started an agent does not read "not on the
# board yet" as "gone".
CHILD_GRACE_SECONDS = 90
# How much of an agent's output rides along with the news that it changed, so
# the model rarely needs a separate read to decide what to do.
UPDATE_TAIL_CHARS = 1500
# A child in one of these is still busy; any move out of them is news.
BUSY_STATUSES = ("working", "starting")
# Free text the model supplies is truncated wherever it is stored or shown.
MAX_TASK = 20_000
MAX_NOTE = 4_000
# A goal, a message to the user and a finish summary are stored rather than
# just logged, so they get the definition's larger ceiling.
MAX_GOAL_TEXT = orchestrator.MAX_GOAL


class Host:
    """Everything an orchestrator can do to this machine, and nothing else.

    Implemented by the server (which owns the fleet poller and the spawn
    boundary) and by fakes in the tests. Methods raise ValueError with a
    message meant for the model when a request is refused; the run turns that
    into a failed tool result and lets the model decide what to do instead.
    """

    def capabilities(self) -> dict:
        """``{"interactive": bool, "engines": [...], "note": str}``.

        `interactive` is False on a host with no scriptable Terminal -- an
        always-on Linux box -- and the tool menu then offers background agents
        only, rather than letting the model ask for something that cannot work.
        """
        raise NotImplementedError

    def projects(self) -> list[dict]:
        """The directories an agent may be started in: ``{"path", "name"}``."""
        raise NotImplementedError

    def start_agent(self, request: dict) -> dict:
        """Start one agent. Returns ``{"message", "jobId", "cwd", "name", ...}``."""
        raise NotImplementedError

    def agents(self) -> list[dict]:
        """Live sessions: ``{"sessionId", "jobId", "title", "status", ...}``."""
        raise NotImplementedError

    def read_output(self, session_id: str, chars: int) -> str:
        """The tail of a session's transcript as plain text."""
        raise NotImplementedError

    def send_to_agent(self, session_id: str, message: str) -> str:
        """Queue a message onto a running session's chat; returns a confirmation."""
        raise NotImplementedError

    def saved_agents(self) -> list[dict]:
        """The agent library: ``{"name", "engine", "model", "systemPrompt"}``."""
        raise NotImplementedError

    def prompts(self) -> list[dict]:
        """The prompt library: ``{"name", "description", "body"}``."""
        raise NotImplementedError

    def skills(self) -> list[dict]:
        """Installed Claude Code skills: ``{"name", "description"}``."""
        raise NotImplementedError

    def read_skill(self, name: str) -> str:
        """One skill's SKILL.md, or ValueError."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Context. Keep the system prompt and the goal, then as much of the tail as
# fits, never starting the tail on an orphan tool result.


def trim_messages(messages: list[dict], budget: int = CONTEXT_BUDGET) -> list[dict]:
    if len(messages) <= 2:
        return list(messages)
    head, tail = messages[:2], messages[2:]
    kept: list[dict] = []
    used = sum(len(json.dumps(m)) for m in head)
    for message in reversed(tail):
        size = len(json.dumps(message))
        if used + size > budget and kept:
            break
        used += size
        kept.insert(0, message)
    # A tool result whose assistant message was dropped is a protocol error;
    # drop those until the window starts on a real turn.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return head + kept


def repair_messages(messages: list[dict]) -> list[dict]:
    """Drop an unfinished final turn, so an interrupted run can be resumed.

    A turn is unfinished when the model asked for tools and not every request
    got a result: the API refuses that conversation, and re-running the tools
    could repeat a side effect. The turn is removed instead, and the caller
    tells the model to look before acting.
    """
    repaired = list(messages)
    while repaired:
        last_assistant = next((i for i in range(len(repaired) - 1, -1, -1)
                               if repaired[i].get("role") == "assistant"), None)
        if last_assistant is None:
            break
        calls = repaired[last_assistant].get("tool_calls") or []
        if not calls:
            break
        answered = {m.get("tool_call_id") for m in repaired[last_assistant + 1:]
                    if m.get("role") == "tool"}
        if all(str(c.get("id")) in answered for c in calls):
            break
        del repaired[last_assistant:]
    return repaired


class Run:
    """One orchestrator's execution: durable state, a worker thread, events.

    Only one worker runs at a time. It exits whenever the run stops making
    progress -- finished, failed, stopped, waiting for an approval, or waiting
    for the user to reply -- and is started again by whatever unblocks it.
    """

    def __init__(self, definition: orchestrator.Orchestrator, host: Host) -> None:
        self.definition = definition
        self.host = host
        self.state = orchestrator.read_state(definition.id)
        self._lock = threading.RLock()
        self._subscribers: list[queue.Queue] = []
        self._cancel = threading.Event()
        self._wake = threading.Event()        # interrupts wait_for_agents
        self._worker: threading.Thread | None = None
        self._active = False                  # a worker is alive and owns this run
        self._deleted = False                 # the definition is gone; stop writing
        self._decision: dict | None = None    # an answered approval, applied by the worker
        self._finish_summary: str | None = None
        # What the worker is doing right now, for the panel's status line.
        # In memory only: after a restart there is nothing in flight to show.
        self.phase = ""                       # thinking | acting | watching | ""
        self._busy_agents = 0
        self._last_change = 0.0

    # -- events: the same subscribe/emit shape the chat panel already speaks --

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue()
        with self._lock:
            for event in orchestrator.journal(self.definition.id):
                channel.put(event)            # replay, including across restarts
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def _emit(self, event: dict) -> None:
        stored = orchestrator.append_journal(self.definition.id, event)
        with self._lock:
            channels = list(self._subscribers)
        for channel in channels:
            channel.put(stored)

    # -- state ---------------------------------------------------------------

    def _persist(self) -> None:
        if self._deleted:
            # Its directory was removed; writing would leave an orphan behind.
            return
        orchestrator.write_state(self.definition.id, self.state)

    def snapshot(self) -> dict:
        try:
            persona = self._persona()
        except ValueError:
            persona = None
        with self._lock:
            # The lists are copied, not referenced: the worker appends to them
            # while the HTTP thread is serializing this.
            state = dict(self.state)
            state["children"] = [dict(child) for child in state.get("children") or []]
            state["plan"] = [dict(item) for item in state.get("plan") or []]
        pending = state.get("pending")
        return {
            **self.definition.to_dict(),
            "status": state["status"], "goal": state["goal"], "steps": state["steps"],
            "spawns": state["spawns"], "costUsd": round(float(state.get("costUsd") or 0), 4),
            "children": state["children"], "plan": state["plan"], "reason": state["reason"],
            "lastText": state["lastText"], "startedAt": state["startedAt"],
            "updatedAt": state["updatedAt"], "queued": len(state.get("inbox") or []),
            "pending": ({"id": pending["id"], "tool": pending["call"]["name"],
                         "args": _safe_args(pending["call"]), "reason": pending.get("reason", ""),
                         "at": pending.get("at", 0)} if pending else None),
            "phase": self.phase, "busyAgents": self._busy_agents,
            "lastChangeAt": self._last_change,
            "model": persona.model if persona else "",
            "persona": ({"id": persona.id, "name": persona.name,
                         "agentDefaults": persona.agent_defaults.to_dict(),
                         "allowedModels": list(persona.allowed_models)} if persona else None),
            "memory": orchestrator.memory(self.definition.id),
            "personaMemory": [dict(m) for m in persona.memory] if persona else [],
        }

    # -- lifecycle -----------------------------------------------------------

    def start(self, goal: str) -> None:
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("Give the orchestrator a goal.")
        if len(goal) > orchestrator.MAX_GOAL:
            raise ValueError(f"Keep the goal under {orchestrator.MAX_GOAL:,} characters.")
        with self._lock:
            if self.state["status"] in orchestrator.ACTIVE_STATUSES:
                raise ValueError("This orchestrator is already running. Stop it first.")
            persona = self._persona()
            old = self.state
            self.state = orchestrator.blank_state()
            # A new run is a new conversation, not a new employee: the agents
            # it started before are still its own, and it is told how the last
            # run ended. What it was told lives in memory, which is not reset.
            self.state.update(status="running", goal=goal, startedAt=time.time(),
                              children=list(old.get("children") or []),
                              previous=({"goal": old.get("goal"), "reason": old.get("reason")}
                                        if old.get("goal") else None),
                              messages=[
                                  {"role": "system", "content": ""},   # rebuilt every step
                                  {"role": "user", "content": "Goal:\n" + goal},
                              ])
            self._cancel.clear()
            self._wake.clear()
            self._finish_summary = None
            self._decision = None
            self._persist()
        self._emit({"type": "run_started", "goal": goal[:MAX_NOTE], "model": persona.model,
                    "persona": persona.name, "mode": self.definition.mode})
        self._spin()

    def resume(self) -> None:
        """Pick up a run the server stopped mid-step."""
        with self._lock:
            if not orchestrator.interrupted(self.state):
                return
            self.state["messages"] = repair_messages(self.state["messages"])
            self.state["messages"].append({"role": "user", "content":
                "AgentGrid restarted, so your last step was interrupted. Call list_agents and "
                "list_tickets before starting anything: an agent may already have been started."})
            self._cancel.clear()
            self._wake.clear()
            self._persist()
        self._emit({"type": "resumed", "steps": self.state["steps"]})
        self._spin()

    def stop(self, reason: str = "Stopped by you.") -> None:
        self._cancel.set()
        self._wake.set()
        with self._lock:
            if self.state["status"] in orchestrator.ACTIVE_STATUSES:
                self.state.update(status="stopped", reason=reason, pending=None)
                self._persist()
        self._emit({"type": "run_done", "status": "stopped", "reason": reason})

    def submit(self, text: str) -> None:
        """Queue a message from the user; wake the run if it is not blocked."""
        text = str(text or "").strip()
        if not text:
            raise ValueError("Type a message first.")
        with self._lock:
            if self.state["status"] == "idle" and not self.state["messages"]:
                raise ValueError("Give this orchestrator a goal to start it.")
            self.state["inbox"] = (self.state.get("inbox") or [])[-50:] + [{"at": time.time(), "text": text[:MAX_GOAL_TEXT]}]
            blocked = bool(self.state.get("pending"))
            if not blocked and self.state["status"] != "running":
                self.state.update(status="running", reason="")
            self._persist()
        self._emit({"type": "user_message", "text": text[:MAX_NOTE]})
        self._wake.set()
        if not blocked:
            self._spin()

    def approve(self, approval_id: str, edits: dict | None = None) -> None:
        self._answer(approval_id, {"approved": True, "edits": edits or {}})

    def decline(self, approval_id: str, reason: str = "") -> None:
        self._answer(approval_id, {"approved": False, "reason": str(reason or "")[:MAX_NOTE]})

    def _answer(self, approval_id: str, decision: dict) -> None:
        with self._lock:
            pending = self.state.get("pending")
            if not pending:
                raise ValueError("Nothing is waiting for your approval.")
            if approval_id and approval_id != pending["id"]:
                raise ValueError("That request has already been answered.")
            self._decision = decision
            self.state.update(status="running", reason="")
            self._cancel.clear()
            self._persist()
        self._emit({"type": "approval_resolved", "id": pending["id"],
                    "tool": pending["call"]["name"], "approved": bool(decision.get("approved")),
                    "reason": decision.get("reason", "")})
        self._spin()

    def _spin(self) -> None:
        """Start the worker unless one already owns this run.

        `_active` rather than `is_alive()`: a thread on its way out is still
        alive, and handing the run to a second worker would double every step.
        The flag is only ever changed under the lock, and the outgoing worker
        re-checks the run under that same lock before clearing it.
        """
        with self._lock:
            if self._active:
                return
            self._active = True
            self._worker = threading.Thread(target=self._work, daemon=True)
            self._worker.start()

    def _work(self) -> None:
        try:
            while not self._cancel.is_set():
                if self._step():
                    continue
                with self._lock:
                    # A message or an approval may have landed while this step
                    # was finishing. Checking here, under the lock `_spin`
                    # uses, is what stops that work from sitting untouched
                    # until the next nudge.
                    if self.state["status"] != "running" and self._decision is None:
                        return
        except Exception as error:                     # never leave a run "running"
            self._fail(f"The run stopped unexpectedly: {type(error).__name__}.")
        finally:
            with self._lock:
                self._active = False

    # -- one step ------------------------------------------------------------

    def _step(self) -> bool:
        """Advance the run once. True to keep going, False when it is at rest."""
        with self._lock:
            decision, self._decision = self._decision, None
        if decision is not None:
            return self._apply_decision(decision)

        self._drain_inbox()
        limit = self._limit_hit()
        if limit:
            self._emit({"type": "limit", "message": limit})
            self._finish("failed", limit)
            return False

        try:
            context = self._context()
        except ValueError as error:          # the persona was deleted under it
            self._fail(str(error))
            return False
        with self._lock:
            self.state["messages"][0] = {"role": "system", "content": system_prompt(context)}
        self._set_phase("thinking")
        try:
            turn = openrouter.tool_turn(trim_messages(self.state["messages"]),
                                        context.persona.model, tool_specs(context))
        except ValueError as error:
            self._fail(str(error))
            return False
        except OSError:
            self._fail("Could not reach OpenRouter. The run stopped; start it again when the "
                       "connection is back.")
            return False
        finally:
            self._set_phase("")
        with self._lock:
            self.state["steps"] += 1
            self.state["costUsd"] = float(self.state.get("costUsd") or 0) + float(turn.get("costUsd") or 0)
            self.state["messages"].append(turn["raw"])
            self._persist()
        if not turn["calls"]:
            return self._after_prose(turn["text"])
        if turn["text"].strip():
            # Reasoning said alongside tool calls: the Activity tab's business,
            # not the conversation's.
            self._emit({"type": "note", "text": turn["text"][:MAX_NOTE]})
        self._set_phase("acting")
        try:
            outcome = self._run_calls(turn["calls"])
        finally:
            self._set_phase("")
        return self._handle(outcome)

    def _after_prose(self, text: str) -> bool:
        """A turn that ended in words rather than tool calls.

        With agents still working, the words are a progress note: they go to
        the user, and AgentGrid watches the agents at no cost until one of
        them changes -- the model is woken with the news, never left waiting
        for someone to ask how it is going. With nothing in flight, the
        orchestrator is talking to the user, so the turn is theirs.
        """
        text = text.strip()
        statuses = self._child_statuses()
        if any(status in BUSY_STATUSES for status in statuses.values()):
            if text:
                with self._lock:
                    self.state["lastText"] = text[:MAX_GOAL_TEXT]
                    self._persist()
                self._emit({"type": "message", "text": text[:MAX_NOTE], "waiting": False})
            changes = self._watch(baseline=statuses)
            if changes:
                self._tell_model(changes)
            # No changes means the wait was cut short -- by a message from the
            # user, which the next step reads, or by a stop, which ends it.
            return not self._cancel.is_set()
        with self._lock:
            self.state.update(status="waiting", lastText=text[:MAX_GOAL_TEXT])
            self._persist()
        self._emit({"type": "message", "text": text[:MAX_NOTE], "waiting": True})
        return False

    def _handle(self, outcome: str) -> bool:
        if outcome == "finished":
            summary, self._finish_summary = self._finish_summary, None
            # Consumed here: left armed, it would end the next run the user
            # starts with a message at its first tool call.
            self._finish("done", summary or "Finished.")
            return False
        if outcome in ("gated", "stopped"):
            return False
        return True

    def _run_calls(self, calls: list[dict]) -> str:
        for index, call in enumerate(calls):
            if self._cancel.is_set():
                return "stopped"
            if call.get("name") == "start_agent":
                call = self._shape_start(call)
            gate = self._gate_reason(call)
            if gate:
                self._request_approval(call, calls[index + 1:], gate)
                return "gated"
            self._record_result(call, self._invoke(call))
            if self._finish_summary is not None:
                return "finished"
        return "done"

    def _apply_decision(self, decision: dict) -> bool:
        """Resolve the pending approval, then finish the rest of that turn."""
        with self._lock:
            pending = self.state.get("pending")
            self.state["pending"] = None
            self._persist()
        if not pending:
            return True
        call, remaining = pending["call"], pending.get("remaining") or []
        if decision.get("approved"):
            edits = decision.get("edits") or {}
            if edits:
                call = _with_edits(call, edits)
            result = self._invoke(call)
        else:
            reason = decision.get("reason") or "no reason given"
            result = {"ok": False, "error": f"The user declined this request: {reason}. "
                                            f"Do not retry it unchanged."}
            self._emit({"type": "tool", "name": call["name"], "ok": False, "summary": "declined"})
        self._record_result(call, result)
        if self._finish_summary is not None:
            return self._handle("finished")
        return self._handle(self._run_calls(remaining))

    def _request_approval(self, call: dict, remaining: list[dict], reason: str) -> None:
        pending = {"id": secrets.token_hex(8), "call": call, "remaining": remaining,
                   "reason": reason, "at": time.time()}
        with self._lock:
            self.state.update(status="waiting", pending=pending)
            self._persist()
        self._emit({"type": "approval_requested", "id": pending["id"], "tool": call["name"],
                    "reason": reason, "args": _safe_args(call)})

    def _record_result(self, call: dict, result: dict) -> None:
        payload = json.dumps(result, ensure_ascii=False)
        if len(payload) > MAX_TOOL_RESULT:
            payload = json.dumps({"ok": result.get("ok", True), "truncated": True,
                                  "content": payload[:MAX_TOOL_RESULT]}, ensure_ascii=False)
        with self._lock:
            self.state["messages"].append({"role": "tool", "tool_call_id": call.get("id") or "",
                                           "name": call["name"], "content": payload})
            self._persist()

    def _finish(self, status: str, reason: str) -> None:
        with self._lock:
            self.state.update(status=status, reason=reason[:MAX_NOTE], pending=None)
            self._persist()
        self._emit({"type": "run_done", "status": status, "reason": reason[:MAX_NOTE]})

    def _fail(self, reason: str) -> None:
        self._emit({"type": "error", "message": reason[:MAX_NOTE]})
        self._finish("failed", reason)

    def _drain_inbox(self) -> None:
        with self._lock:
            inbox = self.state.get("inbox") or []
            if not inbox:
                return
            self.state["inbox"] = []
            for item in inbox:
                self.state["messages"].append({"role": "user", "content": str(item.get("text") or "")})
            self._persist()
        # The wake flag exists to cut a `wait_for_agents` short; the message
        # that set it has now been read, so the next wait is a real wait.
        self._wake.clear()

    def _limit_hit(self) -> str:
        limits = self.definition.limits
        if self.state["steps"] >= limits.max_steps:
            return f"Step limit reached ({limits.max_steps} steps). Raise it or start a new run."
        # Cost is what OpenRouter reported for the steps so far. A provider
        # that reports none leaves this at zero, and the step limit is then
        # the only ceiling -- which is why there is a step limit.
        spent = float(self.state.get("costUsd") or 0)
        if spent >= limits.max_spend_usd:
            return (f"Spend limit reached (${spent:.2f} of ${limits.max_spend_usd:.2f}). "
                    f"Raise it or start a new run.")
        return ""

    def _gate_reason(self, call: dict) -> str:
        """Why this call needs the user, or "" when it may just run."""
        name = call.get("name")
        if name == "start_agent":
            args = _parse_args(call) or {}
            try:
                persona = self._persona()
            except ValueError:
                return ""
            interactive = str(args.get("mode") or "") == "interactive"
            # A request that will be refused -- a model outside the fence, an
            # unknown saved agent, a terminal this host lacks -- has nothing
            # to approve: refusing it outright is a better answer than asking
            # the user to authorise a failure.
            if self._start_problem(args, persona) or (
                    interactive and not self.host.capabilities().get("interactive")):
                return ""
            terminal = "opens an interactive Terminal session on your desktop"
            if self.definition.mode == "ask":
                return terminal if interactive else "starts a new agent"
            # Auto still asks before putting a window in front of the user --
            # unless their persona says interactive is how they want agents.
            if interactive and persona.agent_defaults.mode != "interactive":
                return terminal
        if name == "create_work_area" and self.definition.mode == "ask":
            return "creates a work area"
        return ""

    # -- tools ---------------------------------------------------------------

    def _invoke(self, call: dict) -> dict:
        handler = getattr(self, "_tool_" + str(call.get("name") or ""), None)
        if handler is None or not str(call.get("name") or "").isidentifier():
            return {"ok": False, "error": f"There is no tool called {str(call.get('name'))[:60]!r}."}
        args = _parse_args(call)
        if args is None:
            return {"ok": False, "error": "Those arguments were not valid JSON. Send the same call "
                                          "again with correct JSON."}
        try:
            result = handler(args)
        except ValueError as error:
            result = {"ok": False, "error": str(error)}
        except Exception:
            # A tool bug must read as a failed tool, not crash the run, and
            # must not hand the model a traceback from this process.
            result = {"ok": False, "error": "That tool failed on this machine."}
        self._emit({"type": "tool", "name": call["name"], "ok": bool(result.get("ok", True)),
                    "summary": _summarize(call["name"], args, result)})
        return result

    def _tool_list_projects(self, args: dict) -> dict:
        return {"ok": True, "projects": [{"path": p["path"], "name": p.get("name") or p["path"]}
                                         for p in self.host.projects()[:200]],
                "yourProject": self.definition.cwd}

    def _tool_list_work_areas(self, args: dict) -> dict:
        data = areas.load()
        return {"ok": True, "yourScope": self.definition.scope or "all work areas",
                "areas": [{"id": a["id"], "name": a["name"]} for a in data.get("areas", [])]}

    def _tool_create_work_area(self, args: dict) -> dict:
        if self.definition.scope:
            raise ValueError("You belong to one work area and cannot create others.")
        name = str(args.get("name") or "").strip()
        data = areas.update({"action": "save", "name": name})
        created = next((a for a in data["areas"] if a["name"].casefold() == name.casefold()), None)
        return {"ok": True, "area": created}

    def _tool_start_agent(self, args: dict) -> dict:
        persona = self._persona()
        args = self._shape_args(dict(args), persona)
        problem = self._start_problem(args, persona)
        if problem:
            raise ValueError(problem)
        saved = self._saved_agent(args.get("savedAgent"), persona)
        limits = self.definition.limits
        if self.state["spawns"] >= limits.max_spawns:
            raise ValueError(f"You have started {self.state['spawns']} agents, which is this run's "
                             f"limit. Finish with the agents you have.")
        running = self._running_children()
        if len(running) >= limits.max_concurrent:
            raise ValueError(f"{len(running)} of your agents are still running, which is the limit. "
                             f"Use wait_for_agents and read_agent_output before starting another.")
        interactive = str(args.get("mode") or "background") == "interactive"
        if interactive and not self.host.capabilities().get("interactive"):
            raise ValueError("This host cannot open interactive sessions. Start it in the "
                             "background instead.")
        task = str(args.get("task") or "").strip()
        if not task:
            raise ValueError("An agent needs a task.")
        # An unnamed agent is numbered, so it never shares a name with the
        # orchestrator itself; a saved agent keeps the name it was saved under.
        name = self._child_name(str(args.get("name") or "").strip()
                                or (saved["name"] if saved else
                                    f"{self.definition.name} {self.state['spawns'] + 1}"))
        standing = "\n\n".join(part for part in (
            (saved or {}).get("systemPrompt") or "", str(args.get("systemPrompt") or "")) if part.strip())
        request = {
            "cwd": str(args.get("project") or self.definition.cwd or ""),
            "prompt": task[:MAX_TASK],
            "engine": str(args.get("engine") or "claude"),
            "interactive": interactive,
            "model": str(args.get("model") or ""),
            "systemPrompt": standing[:MAX_TASK],
            "name": name[:orchestrator.MAX_NAME],
            # Its own area wins: an orchestrator that belongs to one area has no
            # authority to file work into another.
            "areaId": self.definition.scope or str(args.get("areaId") or ""),
            "ticketKey": str(args.get("ticketKey") or ""),
        }
        launched = self.host.start_agent(request)
        child = {"name": request["name"], "cwd": launched.get("cwd") or request["cwd"],
                 "engine": request["engine"], "interactive": request["interactive"],
                 "model": request["model"], "savedAgent": saved["name"] if saved else "",
                 "jobId": launched.get("jobId") or "", "sessionId": "",
                 "task": task[:MAX_NOTE], "at": time.time()}
        with self._lock:
            # Written before the result reaches the model: if the server dies
            # here, the record of this agent survives and the resume note
            # tells the model to look for it.
            self.state["children"] = (self.state.get("children") or []) + [child]
            self.state["spawns"] += 1
            self._persist()
        self._emit({"type": "agent_started", "name": child["name"], "cwd": child["cwd"],
                    "engine": child["engine"], "interactive": child["interactive"],
                    "model": child["model"], "savedAgent": child["savedAgent"],
                    "task": task[:MAX_NOTE]})
        return {"ok": True, "message": launched.get("message") or "Started.",
                "agent": {"name": child["name"], "jobId": child["jobId"], "cwd": child["cwd"]}}

    def _tool_list_agents(self, args: dict) -> dict:
        mine = {c.get("name") for c in self.state.get("children") or []}
        agents = []
        for session in self.host.agents()[:100]:
            title = session.get("title") or session.get("customName") or ""
            agents.append({"sessionId": session.get("sessionId"), "name": title,
                           "status": session.get("status"), "engine": session.get("engine"),
                           "cwd": session.get("cwd"), "kind": session.get("kind"),
                           "idleSeconds": session.get("idleSeconds"),
                           "yours": title in mine or session.get("jobId") in
                                    {c.get("jobId") for c in self.state.get("children") or [] if c.get("jobId")}})
        return {"ok": True, "agents": agents,
                "yourAgentsStarted": self.state["spawns"],
                "limits": {"atOnce": self.definition.limits.max_concurrent,
                           "perRun": self.definition.limits.max_spawns}}

    def _tool_read_agent_output(self, args: dict) -> dict:
        session_id, name = self._resolve_agent(args.get("agent"))
        try:
            chars = max(500, min(int(args.get("chars") or 6000), MAX_TOOL_RESULT))
        except (TypeError, ValueError):
            chars = 6000
        return {"ok": True, "agent": name, "sessionId": session_id,
                "output": self.host.read_output(session_id, chars)}

    def _tool_send_to_agent(self, args: dict) -> dict:
        session_id, name = self._resolve_agent(args.get("agent"))
        message = str(args.get("message") or "").strip()
        if not message:
            raise ValueError("Say what to send.")
        return {"ok": True, "agent": name,
                "message": self.host.send_to_agent(session_id, message[:MAX_TASK])}

    def _tool_wait_for_agents(self, args: dict) -> dict:
        try:
            seconds = max(1, min(int(args.get("seconds") or 30), MAX_WAIT_SECONDS))
        except (TypeError, ValueError):
            seconds = 30
        started = time.monotonic()
        changes = self._watch(limit=seconds)
        return {"ok": True, "waitedSeconds": round(time.monotonic() - started),
                "changed": self._describe(changes),
                "interrupted": not changes and time.monotonic() - started < seconds - WAIT_CHUNK_SECONDS,
                **self._tool_list_agents({})}

    def _tool_list_tickets(self, args: dict) -> dict:
        try:
            limit = max(1, min(int(args.get("limit") or 50), 200))
        except (TypeError, ValueError):
            limit = 50
        found = tickets.query(status=str(args.get("status") or ""),
                              project=str(args.get("project") or ""),
                              area=str(args.get("area") or self.definition.scope or ""),
                              assignee=str(args.get("assignee") or ""),
                              text=str(args.get("text") or ""), limit=limit)
        return {"ok": True, "tickets": [{"id": t["id"], "title": t["title"], "status": t["status"],
                                         "type": t["type"], "priority": t["priority"],
                                         "assignee": t["assignee"], "area": t.get("area", ""),
                                         "project": t.get("project", "")} for t in found]}

    def _tool_create_ticket(self, args: dict) -> dict:
        ticket = tickets.create(
            str(args.get("title") or ""), body=str(args.get("body") or ""),
            type=str(args.get("type") or "task"), status=str(args.get("status") or "todo"),
            priority=str(args.get("priority") or "medium"),
            project=str(args.get("project") or self.definition.cwd or ""),
            area=str(args.get("area") or self.definition.scope or ""),
            reporter=self.definition.name)
        return {"ok": True, "ticket": {"id": ticket["id"], "title": ticket["title"],
                                       "status": ticket["status"]}}

    def _tool_move_ticket(self, args: dict) -> dict:
        ticket = tickets.move(str(args.get("ticket") or ""), str(args.get("status") or ""),
                              who=self.definition.name)
        return {"ok": True, "ticket": {"id": ticket["id"], "status": ticket["status"]}}

    def _tool_comment_ticket(self, args: dict) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("Say what the comment should be.")
        ticket = tickets.comment(str(args.get("ticket") or ""), self.definition.name, text[:MAX_TASK])
        return {"ok": True, "ticket": {"id": ticket["id"]}}

    def _tool_set_plan(self, args: dict) -> dict:
        raw = args.get("items")
        if not isinstance(raw, list):
            raise ValueError("Send items as a list.")
        plan = [{"text": str(item.get("text") or "")[:400], "done": bool(item.get("done"))}
                for item in raw[:50] if isinstance(item, dict) and str(item.get("text") or "").strip()]
        with self._lock:
            self.state["plan"] = plan
            self._persist()
        self._emit({"type": "plan", "items": plan})
        return {"ok": True, "items": plan}

    def _tool_remember(self, args: dict) -> dict:
        """Save a lesson on the persona or a fact on this posting, and say so.

        The model chooses where; the user sees the choice in the chat with an
        undo, which is what makes letting it choose safe.
        """
        scope = "persona" if args.get("scope") == "persona" else "posting"
        if scope == "persona":
            persona = self._persona()
            item, owner = personas.remember(persona.id, str(args.get("text") or "")), persona.name
        else:
            item, owner = orchestrator.remember(self.definition.id, str(args.get("text") or "")), self.definition.name
        self._emit({"type": "memory_saved", "scope": scope, "id": item["id"],
                    "text": item["text"], "owner": owner})
        return {"ok": True, "remembered": item["text"], "scope": scope}

    def _tool_read_skill(self, args: dict) -> dict:
        persona, name = self._persona(), str(args.get("name") or "")
        if name not in persona.skills:
            raise ValueError(f"{name[:60]!r} is not one of your skills. Yours: "
                             f"{', '.join(persona.skills) or 'none'}.")
        return {"ok": True, "name": name, "text": self.host.read_skill(name)}

    def _tool_read_prompt(self, args: dict) -> dict:
        persona, name = self._persona(), str(args.get("name") or "")
        if name not in persona.prompts:
            raise ValueError(f"{name[:60]!r} is not one of your prompts. Yours: "
                             f"{', '.join(persona.prompts) or 'none'}.")
        prompt = next((p for p in self.host.prompts() if p["name"] == name), None)
        if prompt is None:
            raise ValueError(f"{name} is no longer in the prompt library.")
        return {"ok": True, "name": name, "text": prompt.get("body") or ""}

    def _tool_message_user(self, args: dict) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("Say something.")
        with self._lock:
            self.state["lastText"] = text[:MAX_GOAL_TEXT]
            self._persist()
        self._emit({"type": "message", "text": text[:MAX_NOTE], "waiting": False})
        return {"ok": True, "delivered": True}

    def _tool_finish(self, args: dict) -> dict:
        summary = str(args.get("summary") or "").strip() or "Finished."
        self._finish_summary = summary[:MAX_GOAL_TEXT]
        return {"ok": True, "finished": True}

    # -- helpers -------------------------------------------------------------

    def _persona(self) -> personas.Persona:
        """Who this posting is, read fresh: an edit in the bank applies at the
        next step, the same way a change of mode does."""
        return personas.get(self.definition.persona_id)

    def _context(self) -> Context:
        """Everything the brief is built from, resolved against what exists.

        A saved agent, skill or prompt the persona names but that has since
        been removed is simply left out -- the persona is not broken by a
        library that changed under it.
        """
        persona = self._persona()
        saved = {a["name"].casefold(): a for a in self.host.saved_agents()}
        installed = {s["name"]: s for s in self.host.skills()}
        library = {p["name"]: p for p in self.host.prompts()}
        return Context(
            posting=self.definition, persona=persona, capabilities=self.host.capabilities(),
            scope_name=self._scope_name(),
            team=[saved[n.casefold()] for n in persona.team if n.casefold() in saved],
            skills=[installed[n] for n in persona.skills if n in installed],
            prompts=[library[n] for n in persona.prompts if n in library],
            posting_memory=orchestrator.memory(self.definition.id),
            previous=self.state.get("previous"))

    def _saved_agent(self, name: object, persona: personas.Persona) -> dict | None:
        wanted = str(name or "").strip().casefold()
        if not wanted or wanted not in {n.casefold() for n in persona.team}:
            return None
        return next((a for a in self.host.saved_agents() if a["name"].casefold() == wanted), None)

    def _shape_args(self, args: dict, persona: personas.Persona) -> dict:
        """Fill in what the model left out, from the persona.

        A saved agent brings its own engine and model -- the user chose them
        when they saved it. Otherwise the persona's defaults apply. Mode
        always defaults from the persona. Idempotent, so it is safe to apply
        both when a call is gated and again when it runs.
        """
        defaults = persona.agent_defaults
        saved = self._saved_agent(args.get("savedAgent"), persona)
        if saved:
            args["engine"], args["model"] = saved["engine"], saved.get("model") or ""
        else:
            args["engine"] = args.get("engine") or defaults.engine
            args["model"] = args.get("model") or defaults.model
        args["mode"] = args.get("mode") or defaults.mode
        return args

    def _shape_start(self, call: dict) -> dict:
        args = _parse_args(call)
        try:
            persona = self._persona()
        except ValueError:
            return call
        if args is None:
            return call
        return {**call, "arguments": json.dumps(self._shape_args(args, persona))}

    def _start_problem(self, args: dict, persona: personas.Persona) -> str:
        """Why this start would be refused, or "". Checked before approval."""
        if args.get("savedAgent"):
            if self._saved_agent(args.get("savedAgent"), persona) is None:
                return (f"{str(args['savedAgent'])[:60]!r} is not one of your saved agents. "
                        f"Your team: {', '.join(persona.team) or 'none'}.")
            return ""                   # its model was the user's choice; not fenced
        model = str(args.get("model") or "")
        if persona.allowed_models and not persona.allows(model):
            return (f"{model or 'The default model'} is not allowed for {persona.name}. "
                    f"Allowed: {', '.join(persona.allowed_models)}. Leave model out to use "
                    f"{persona.agent_defaults.model}.")
        return ""

    def _child_name(self, base: str) -> str:
        """A board name no other agent of this posting has, so it can always
        be found again by name -- an interactive agent has no job id."""
        taken = {c.get("name") for c in self.state.get("children") or []}
        base = base[:orchestrator.MAX_NAME - 3] or self.definition.name
        if base not in taken:
            return base
        number = 2
        while f"{base} {number}" in taken:
            number += 1
        return f"{base} {number}"

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def _child_statuses(self) -> dict:
        """Each child's board status by name.

        `starting` covers the gap between launch and the fleet first seeing
        it; `gone` is a child that has left the board. Also caches how many
        are busy, which is what the status line shows.
        """
        resolved = self._sessions_by_child()
        now = time.time()
        statuses = {}
        for child in self.state.get("children") or []:
            session = resolved.get(child["name"])
            if session:
                statuses[child["name"]] = str(session.get("status") or "")
            elif now - float(child.get("at") or 0) < CHILD_GRACE_SECONDS:
                statuses[child["name"]] = "starting"
            else:
                statuses[child["name"]] = "gone"
        with self._lock:
            self._busy_agents = sum(status in BUSY_STATUSES for status in statuses.values())
        return statuses

    def _watch(self, limit: float | None = None, baseline: dict | None = None) -> list[dict]:
        """Wait, for free, until one of its agents changes.

        Returns ``[{"name", "from", "to"}]``, or ``[]`` when the wait ended
        for another reason: a message from the user, a stop, or `limit`.
        An agent being picked up (starting -> working) is expected, not news.
        """
        before = dict(baseline) if baseline is not None else self._child_statuses()
        started = time.monotonic()
        self._set_phase("watching")
        try:
            while not self._cancel.is_set():
                remaining = None if limit is None else limit - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    return []
                pause = WATCH_POLL_SECONDS if remaining is None else min(WATCH_POLL_SECONDS, remaining)
                if self._wake.wait(max(0.0, pause)):
                    return []
                now = self._child_statuses()
                changes = [{"name": name, "from": before.get(name, ""), "to": status}
                           for name, status in now.items()
                           if status != before.get(name)
                           and not (before.get(name) == "starting" and status == "working")]
                if changes:
                    with self._lock:
                        self._last_change = time.time()
                    return changes
                before.update(now)
            return []
        finally:
            self._set_phase("")

    def _describe(self, changes: list[dict]) -> list[dict]:
        """Announce each change on the board and attach what the agent said.

        The tail rides along so the model can usually decide without a
        separate read -- one call per change instead of two.
        """
        described = []
        resolved = self._sessions_by_child() if changes else {}
        for change in changes:
            entry = dict(change)
            session = resolved.get(change["name"])
            if session and change["to"] not in BUSY_STATUSES:
                try:
                    entry["output"] = self.host.read_output(session.get("sessionId") or "",
                                                            UPDATE_TAIL_CHARS)
                except ValueError:
                    pass
            self._emit({"type": "agent_update", "name": change["name"],
                        "from": change["from"], "to": change["to"]})
            described.append(entry)
        return described

    def _tell_model(self, changes: list[dict]) -> None:
        """Wake the model with what changed while it was not looking."""
        lines = ["AgentGrid update — your agents changed while you waited:"]
        for entry in self._describe(changes):
            lines.append(f"- {entry['name']}: {entry['from'] or 'new'} → {entry['to']}")
            if entry.get("output"):
                lines.append("  Last output:\n  " + entry["output"].strip().replace("\n", "\n  "))
        lines.append("Decide what happens next. If it matters to the user, tell them with "
                     "message_user — they should not have to ask.")
        with self._lock:
            self.state["messages"].append({"role": "user", "content": "\n".join(lines)})
            self._persist()

    def _sessions_by_child(self) -> dict:
        """Map each child record to a live session, by job id, then by name."""
        sessions = self.host.agents()
        by_job = {s.get("jobId"): s for s in sessions if s.get("jobId")}
        by_title = {}
        for session in sessions:
            for key in (session.get("customName"), session.get("title")):
                if key:
                    by_title.setdefault(key, session)
        resolved, changed = {}, False
        with self._lock:
            for child in self.state.get("children") or []:
                session = by_job.get(child.get("jobId")) or by_title.get(child.get("name"))
                if session:
                    resolved[child["name"]] = session
                    if child.get("sessionId") != session.get("sessionId"):
                        child["sessionId"] = session.get("sessionId") or ""
                        changed = True
            if changed:
                self._persist()
        return resolved

    def _running_children(self) -> list[dict]:
        resolved = self._sessions_by_child()
        return [s for s in resolved.values() if s.get("status") in ("working", "blocked")]

    def _resolve_agent(self, value: object) -> tuple[str, str]:
        """A session id, or the name of one, to (session_id, name)."""
        wanted = str(value or "").strip()
        if not wanted:
            raise ValueError("Name the agent, by session id or by the name you gave it.")
        sessions = self.host.agents()
        for session in sessions:
            if session.get("sessionId") == wanted:
                return wanted, session.get("title") or wanted
        resolved = self._sessions_by_child()
        if wanted in resolved:
            session = resolved[wanted]
            return session.get("sessionId") or "", session.get("title") or wanted
        for session in sessions:
            if wanted in (session.get("customName"), session.get("title")):
                return session.get("sessionId") or "", session.get("title") or wanted
        known = [s.get("title") for s in sessions[:20] if s.get("title")]
        raise ValueError(f"No agent called {wanted[:60]!r} is on the board. Running now: "
                         f"{', '.join(known) or 'none'}.")

    def _scope_name(self) -> str:
        if not self.definition.scope:
            return "every work area on this board"
        area = next((a for a in areas.load().get("areas", [])
                     if a["id"] == self.definition.scope), None)
        return f"the \"{area['name']}\" work area" if area else "a work area that no longer exists"



def _parse_args(call: dict) -> dict | None:
    try:
        parsed = json.loads(call.get("arguments") or "{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _safe_args(call: dict) -> dict:
    """The call's arguments for display; never raises, never unbounded."""
    args = _parse_args(call) or {}
    trimmed = {}
    for key, value in list(args.items())[:20]:
        trimmed[str(key)[:40]] = (value[:MAX_NOTE] if isinstance(value, str)
                                  else value if isinstance(value, (int, float, bool)) else str(value)[:MAX_NOTE])
    return trimmed


def _with_edits(call: dict, edits: dict) -> dict:
    """Apply the user's edits to a pending call before it runs."""
    args = _parse_args(call) or {}
    for key, value in (edits or {}).items():
        if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
            args[key] = value
    return {**call, "arguments": json.dumps(args)}


def _summarize(name: str, args: dict, result: dict) -> str:
    """One line for the Activity tab: what was asked, and how it went."""
    if not result.get("ok", True):
        return f"failed: {str(result.get('error') or '')[:160]}"
    if name == "start_agent":
        who = args.get("savedAgent") or args.get("name") or "agent"
        return f"{who} in {args.get('project') or ''} · {args.get('model') or 'default'} · {args.get('mode') or ''}"[:160]
    if name == "remember":
        return f"{args.get('scope')}: {args.get('text') or ''}"[:160]
    if name in ("read_skill", "read_prompt"):
        return str(args.get("name") or "")[:80]
    if name in ("read_agent_output", "send_to_agent"):
        return str(args.get("agent") or "")[:80]
    if name in ("move_ticket", "comment_ticket"):
        return f"{args.get('ticket') or ''} {args.get('status') or ''}".strip()[:80]
    if name == "create_ticket":
        return str((result.get("ticket") or {}).get("id") or "")[:40]
    if name == "wait_for_agents":
        return f"{result.get('waitedSeconds')}s"
    return ""


class RunManager:
    """Every orchestrator run in this process, keyed by orchestrator id.

    A run outlives the browser: nothing here is per-connection. `resume_all` is
    called once at startup so a run interrupted by a restart continues.
    """

    def __init__(self, host: Host) -> None:
        self.host = host
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def run(self, definition: orchestrator.Orchestrator) -> Run:
        """The run for this orchestrator, created on demand. The definition is
        refreshed every time, so editing the mode or the limits applies to a
        run already in flight."""
        with self._lock:
            existing = self._runs.get(definition.id)
            if existing is None:
                existing = Run(definition, self.host)
                self._runs[definition.id] = existing
            else:
                existing.definition = definition
            return existing

    def get(self, orchestrator_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(orchestrator_id)

    def forget(self, orchestrator_id: str) -> None:
        """Stop a run and let go of it, ahead of deleting its definition."""
        with self._lock:
            run = self._runs.pop(orchestrator_id, None)
        if run is not None:
            run.stop("Orchestrator deleted.")
            run._deleted = True

    def resume_all(self) -> list[str]:
        """Restart every run the last server stop interrupted. Returns names."""
        resumed = []
        for definition in orchestrator.list_all():
            state = orchestrator.read_state(definition.id)
            if not orchestrator.interrupted(state):
                continue
            try:
                self.run(definition).resume()
                resumed.append(definition.name)
            except (ValueError, OSError):
                continue
        return resumed
