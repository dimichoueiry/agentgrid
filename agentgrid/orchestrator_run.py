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
- **The state is written after every step.** A run interrupted mid-step (the
  lid closed) resumes from the last completed step: the unfinished model turn
  is dropped and the model is told to check what already exists before it
  starts anything, because a spawn is a side effect that must never be
  silently repeated.

What the model is offered and told lives next door, in
`orchestrator_brief`: the menu and the standing prompt are wording, and this
module is behaviour.

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

from agentgrid import areas, openrouter, orchestrator, tickets
from agentgrid.orchestrator_brief import MAX_WAIT_SECONDS, system_prompt, tool_specs

# A tool result is context for the next decision, not an archive: a 200k-line
# transcript summarised into 20k characters still says what happened.
MAX_TOOL_RESULT = 20_000
# How much conversation a request may carry. The system prompt and the goal are
# always kept; the middle is dropped before the tail.
CONTEXT_BUDGET = 120_000
WAIT_CHUNK_SECONDS = 2.0
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
            capabilities = self.host.capabilities()
            self.state = orchestrator.blank_state()
            self.state.update(status="running", goal=goal, startedAt=time.time(), messages=[
                {"role": "system", "content": system_prompt(self.definition, capabilities,
                                                            self._scope_name())},
                {"role": "user", "content": "Goal:\n" + goal},
            ])
            self._cancel.clear()
            self._wake.clear()
            self._finish_summary = None
            self._decision = None
            self._persist()
        self._emit({"type": "run_started", "goal": goal[:MAX_NOTE], "model": self.definition.model,
                    "mode": self.definition.mode})
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

        capabilities = self.host.capabilities()
        try:
            turn = openrouter.tool_turn(trim_messages(self.state["messages"]),
                                        self.definition.model,
                                        tool_specs(self.definition, capabilities))
        except ValueError as error:
            self._fail(str(error))
            return False
        except OSError:
            self._fail("Could not reach OpenRouter. The run stopped; start it again when the "
                       "connection is back.")
            return False
        with self._lock:
            self.state["steps"] += 1
            self.state["costUsd"] = float(self.state.get("costUsd") or 0) + float(turn.get("costUsd") or 0)
            self.state["messages"].append(turn["raw"])
            self._persist()
        if turn["text"].strip():
            self._emit({"type": "note", "text": turn["text"][:MAX_NOTE]})
        if not turn["calls"]:
            # Plain prose ends the turn: the orchestrator is talking to the
            # user, so it waits for them rather than spending another step.
            with self._lock:
                self.state.update(status="waiting", lastText=turn["text"][:MAX_GOAL_TEXT])
                self._persist()
            self._emit({"type": "message", "text": turn["text"][:MAX_NOTE], "waiting": True})
            return False
        return self._handle(self._run_calls(turn["calls"]))

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
            if str(args.get("mode") or "") == "interactive":
                # On a host with no scriptable terminal there is nothing to
                # approve: the tool refuses it outright, which is a better
                # answer than asking the user to authorise a failure.
                return ("opens an interactive Terminal session on your desktop"
                        if self.host.capabilities().get("interactive") else "")
            if self.definition.mode == "ask":
                return "starts a new agent"
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
        name = str(args.get("name") or "").strip() or f"{self.definition.name} {self.state['spawns'] + 1}"
        request = {
            "cwd": str(args.get("project") or self.definition.cwd or ""),
            "prompt": task[:MAX_TASK],
            "engine": str(args.get("engine") or "claude"),
            "interactive": interactive,
            "model": str(args.get("model") or ""),
            "systemPrompt": str(args.get("systemPrompt") or "")[:MAX_TASK],
            "name": name[:orchestrator.MAX_NAME],
            # Its own area wins: an orchestrator that belongs to one area has no
            # authority to file work into another.
            "areaId": self.definition.scope or str(args.get("areaId") or ""),
            "ticketKey": str(args.get("ticketKey") or ""),
        }
        launched = self.host.start_agent(request)
        child = {"name": request["name"], "cwd": launched.get("cwd") or request["cwd"],
                 "engine": request["engine"], "interactive": request["interactive"],
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
        waited = self._sleep(seconds)
        return {"ok": True, "waitedSeconds": round(waited),
                "interrupted": waited < seconds - WAIT_CHUNK_SECONDS,
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

    def _sleep(self, seconds: float) -> float:
        """Wait, but stay answerable: a stop or a user message ends it early."""
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if self._cancel.is_set() or self._wake.is_set():
                break
            remaining = seconds - (time.monotonic() - started)
            if self._wake.wait(min(WAIT_CHUNK_SECONDS, remaining)):
                break
        return time.monotonic() - started

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
        return f"{args.get('name') or 'agent'} in {args.get('project') or ''}"[:160]
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
