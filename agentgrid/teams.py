"""Agent teams: wire Claude Code and Codex sessions into a pipeline with loops.

A "team" is the authoring layer AgentGrid was missing. It lets you compose real
Claude Code sessions -- a researcher, a writer, a reviewer -- into a pipeline
where one node's output feeds the next node's input, with loops ("send the draft
back to the writer until the reviewer approves"). It is deliberately NOT a new
agent framework: every node is a real `claude -p` turn on the user's own login,
driven through the same `chat.ChatSession` engine the chat panel uses. No SDK,
no API key, no LangGraph -- Claude Code already is the agent; this only decides
who runs, in what order, and what each one is told.

The design in three ideas:

- **Nodes are sessions.** Each node owns one persistent `ChatSession`. Running a
  node a second time (on a loop) *resumes* that session, so the writer still has
  its draft and genuinely revises it rather than starting from scratch.
- **Wiring is templating.** A node's prompt is a template; `{input}` is the
  team's input and `{node_id}` is that node's latest output. That is the
  "output of one into the input of the next", written plainly.
- **Loops are explicit.** A loop watches one node's output for a token (e.g.
  `NEEDS_WORK`) and, while it is present and under a cap, jumps back to an
  earlier node with a feedback message. Bounded, so a stubborn reviewer can't
  spin forever.

Everything fails soft and streams: the run emits the same shape of events the
chat panel already understands, tagged by node, so the board can draw the
pipeline live.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from agentgrid import chat

TEAMS_DIR = Path.home() / ".agentgrid" / "teams"

_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_-]+)\}")


# ---------------------------------------------------------------------------
# The team definition -- plain data, so a team is authored, not coded.


@dataclass
class Node:
    id: str
    role: str            # the human label shown on the board ("researcher")
    prompt: str          # template over {input} and {node_id}
    posture: str = "auto"
    model: str = ""
    engine: str = "claude"
    instructions: str = ""
    include_context: bool = False


@dataclass
class Loop:
    at: str              # node whose output is tested
    back_to: str         # node to jump back to when the test passes
    when: str            # token that, if present in `at`'s output, triggers the loop
    max: int = 3
    # What the `back_to` node is told on a loop. {at} is the tested node's output.
    feedback: str = "Please revise based on this feedback:\n\n{at}"


@dataclass
class Team:
    name: str
    cwd: str
    nodes: list[Node]
    loops: list[Loop] = field(default_factory=list)
    coordinator: dict | None = None

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def index_of(self, node_id: str) -> int:
        return next(i for i, n in enumerate(self.nodes) if n.id == node_id)


# ---------------------------------------------------------------------------
# Load / validate / store. A malformed team raises ValueError with the reason;
# every file read tolerates absence and corruption like the rest of the app.


def load_team(data: object) -> Team:
    if not isinstance(data, dict):
        raise ValueError("a team must be a JSON object")
    name = str(data.get("name") or "").strip()
    cwd = str(data.get("cwd") or "").strip()
    if not name:
        raise ValueError("the team needs a name")
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("the team needs at least one node")

    nodes: list[Node] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("each node must be an object")
        node_id = str(raw.get("id") or "").strip()
        prompt = str(raw.get("prompt") or "")
        if not node_id or not prompt.strip():
            raise ValueError("each node needs an id and a prompt")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", node_id) or node_id in ("input", "at", "__coordinator"):
            raise ValueError("step ids must use letters, numbers, hyphens or underscores; input and at are reserved")
        if node_id in seen:
            raise ValueError(f"duplicate node id: {node_id}")
        seen.add(node_id)
        posture = raw.get("posture") if raw.get("posture") in chat.POSTURES else chat.DEFAULT_POSTURE
        engine = raw.get("engine", "claude")
        if engine not in ("claude", "codex", "openrouter"):
            raise ValueError(f"{node_id}: choose Claude, Codex or OpenRouter")
        if "includeContext" in raw and not isinstance(raw["includeContext"], bool):
            raise ValueError(f"{node_id}: includeContext must be true or false")
        if engine == "openrouter":
            posture = "read-only"
        if engine == "openrouter" and not str(raw.get("model") or "").strip():
            raise ValueError(f"{node_id}: choose an OpenRouter model")
        nodes.append(Node(
            id=node_id,
            role=str(raw.get("role") or node_id),
            prompt=prompt,
            posture=posture,
            model=str(raw.get("model") or "").strip(),
            engine=engine, instructions=str(raw.get("instructions") or ""),
            include_context=raw.get("includeContext", False),
        ))

    loops: list[Loop] = []
    raw_loops = data.get("loops") or []
    if not isinstance(raw_loops, list):
        raise ValueError("loops must be a list")
    loop_nodes = set()
    for raw in raw_loops:
        if not isinstance(raw, dict):
            raise ValueError("each loop must be an object")
        at = str(raw.get("at") or "")
        back_to = str(raw.get("back_to") or "")
        when = str(raw.get("when") or "")
        if at not in seen or back_to not in seen:
            raise ValueError("a loop's 'at' and 'back_to' must be node ids")
        ids = [n.id for n in nodes]
        if ids.index(back_to) >= ids.index(at):
            raise ValueError("a revision loop must return to an earlier step")
        if at in loop_nodes:
            raise ValueError("each review step can have only one revision loop")
        loop_nodes.add(at)
        if not when.strip():
            raise ValueError("a loop needs a 'when' token to watch for")
        try:
            cap = max(1, int(raw.get("max", 3)))
        except (TypeError, ValueError):
            cap = 3
        loops.append(Loop(
            at=at, back_to=back_to, when=when, max=cap,
            feedback=str(raw.get("feedback") or Loop.feedback),
        ))
    coordinator = data.get("coordinator")
    if coordinator is not None:
        if not isinstance(coordinator, dict) or coordinator.get("engine", "claude") not in ("claude", "codex", "openrouter"):
            raise ValueError("Choose a valid coordinator engine")
        if coordinator.get("engine") == "openrouter" and not str(coordinator.get("model") or "").strip():
            raise ValueError("Choose an OpenRouter coordinator model")
        try:
            cap = int(coordinator.get("maxDelegations", 8))
        except (ValueError, TypeError):
            raise ValueError("Coordinator delegation limit must be a number")
        if not 1 <= cap <= 50:
            raise ValueError("Coordinator delegation limit must be between 1 and 50")
        coordinator = {"engine": coordinator.get("engine", "claude"),
                       "model": str(coordinator.get("model") or ""),
                       "instructions": str(coordinator.get("instructions") or ""), "maxDelegations": cap}
        if loops:
            raise ValueError("Coordinator mode handles revisions itself; remove fixed revision loops")
    return Team(name=name, cwd=cwd, nodes=nodes, loops=loops, coordinator=coordinator)


def team_to_dict(team: Team) -> dict:
    return {
        "name": team.name,
        "cwd": team.cwd,
        "coordinator": team.coordinator,
        "nodes": [
            {"id": n.id, "role": n.role, "prompt": n.prompt,
             "posture": n.posture, "model": n.model, "engine": n.engine,
             "instructions": n.instructions, "includeContext": n.include_context}
            for n in team.nodes
        ],
        "loops": [
            {"at": l.at, "back_to": l.back_to, "when": l.when,
             "max": l.max, "feedback": l.feedback}
            for l in team.loops
        ],
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-") or "team"


def list_teams() -> list[Team]:
    teams: list[Team] = []
    try:
        paths = sorted(TEAMS_DIR.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            teams.append(load_team(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return teams


def save_team(team: Team) -> Path:
    TEAMS_DIR.mkdir(parents=True, exist_ok=True)
    path = TEAMS_DIR / f"{_slug(team.name)}.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(team_to_dict(team), indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def delete_team(name: str) -> bool:
    try:
        (TEAMS_DIR / f"{_slug(name)}.json").unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Templating: {input} and {node_id} -> values. Unknown braces are left as-is,
# so a prompt full of literal { } (JSON, code) is not mangled.


def render(template: str, values: dict) -> str:
    return _PLACEHOLDER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template,
    )


# ---------------------------------------------------------------------------
# Running a team.


class TeamRun:
    """One execution of a team: nodes in order, wired by templating, with loops.

    Each node runs to completion before the next starts (the CLI is one turn at a
    time). A node's `ChatSession` persists for the whole run, so a loop back to
    it resumes -- the writer keeps its draft. Events stream to subscribers tagged
    by node, in the chat panel's own vocabulary.
    """

    def __init__(self, team: Team, team_input: str) -> None:
        self.team = team
        self.outputs: dict[str, str] = {"input": team_input}
        # Every node id starts empty, so a prompt referencing {review} before the
        # reviewer has run renders blank rather than a literal "{review}".
        for node in team.nodes:
            self.outputs.setdefault(node.id, "")
        self._sessions: dict[str, chat.ChatSession] = {}
        self._subscribers: list[queue.Queue] = []
        self._log: list[dict] = []       # every event, so a late viewer sees the whole run
        self._lock = threading.Lock()
        self._cancelled = False
        self._worker: threading.Thread | None = None
        self.done = False
        self.ok = False
        self.reason = ""
        self.final_result = ""

    # -- fan-out (same shape as ChatSession, so the UI reuses its plumbing) --

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue()
        with self._lock:
            for event in self._log:      # replay: open the panel late, still see it all
                channel.put(event)
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def _emit(self, event: dict) -> None:
        with self._lock:
            self._log.append(event)
            channels = list(self._subscribers)
        for channel in channels:
            channel.put(event)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()

    def cancel(self) -> None:
        self._cancelled = True
        for session in list(self._sessions.values()):
            session.cancel()

    def _run(self) -> None:
        self._emit({
            "type": "team_started",
            "name": self.team.name,
            "nodes": [{"id": n.id, "role": n.role} for n in self.team.nodes],
        })
        loop_counts: dict[str, int] = {}
        override: str | None = None      # a one-shot prompt for a loop-back node
        idx = 0
        try:
            if self.team.coordinator:
                self._run_coordinated()
                return
            while idx < len(self.team.nodes) and not self._cancelled:
                node = self.team.nodes[idx]
                prompt = override if override is not None else render(node.prompt, self.outputs)
                override = None
                result, failed = self._run_node(node, prompt)
                self.outputs[node.id] = result
                if failed or self._cancelled:
                    break

                loop = self._loop_at(node.id)
                if loop and loop.when.upper() in result.upper():
                    if loop_counts.get(loop.at, 0) >= loop.max:
                        self.reason = f"Revision limit reached at {loop.at}; review still requests changes."
                        self._emit({"type": "revision_limit", "id": loop.at, "message": self.reason})
                        break
                    loop_counts[loop.at] = loop_counts.get(loop.at, 0) + 1
                    self._emit({"type": "loop", "at": loop.at, "backTo": loop.back_to,
                                "iter": loop_counts[loop.at], "max": loop.max})
                    # {at} is a convenience alias for the tested node's output, so a
                    # generic feedback template needn't hard-code the reviewer's id.
                    override = render(loop.feedback, {**self.outputs, "at": result})
                    idx = self.team.index_of(loop.back_to)
                    continue
                idx += 1
            else:
                self.ok = not self._cancelled
        except Exception as error:
            self.reason = str(error)
            self._emit({"type": "team_error", "message": self.reason})
        finally:
            self.done = True
            self._emit({"type": "team_done", "ok": self.ok,
                        "cancelled": self._cancelled, "outputs": self.outputs, "reason": self.reason, "finalResult": self.final_result})

    def _run_coordinated(self):
        """Bounded delegation through validated JSON decisions, one worker at a time."""
        config = self.team.coordinator
        coordinator = Node(id="__coordinator", role="Coordinator", prompt="Decide the next step.",
                           engine=config["engine"], model=config["model"], posture="read-only")
        roster = [{"id": n.id, "role": n.role, "task": n.prompt} for n in self.team.nodes]
        rules = (
            "Coordinate the workflow using only the named specialists below. Do not use filesystem, shell, or other tools yourself. "
            "Return ONLY one JSON object per turn. To delegate: "
            '{"action":"delegate","agent":"step_id","task":"specific assignment"}. '
            'To finish: {"action":"finish","result":"final answer for the user"}. '
            "Read specialist results before deciding. Request revisions by delegating again. "
            "Do not claim work was done unless it appears in a specialist result. "
            "You cannot create agents or change models or permissions.\n"
        )
        used = 0
        while not self._cancelled:
            # Only bounded excerpts go into coordinator decisions; full originals
            # stay in outputs and are passed to the selected specialist.
            results = {k: v[-20000:] for k, v in self.outputs.items() if k != "input" and v}
            prompt = rules + config["instructions"] + "\nOriginal brief:\n" + self.outputs["input"]
            prompt += "\nSpecialists:\n" + json.dumps(roster)
            prompt += "\nLatest results (long results may be excerpted):\n" + json.dumps(results)
            prompt += f"\nRemaining delegations: {config['maxDelegations'] - used}."
            text, failed = self._run_node(coordinator, prompt)
            if failed or self._cancelled:
                self.reason = "Coordinator failed or was stopped."
                return
            candidate = text.strip()
            if candidate.startswith("```"):
                candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate)
            try:
                decision = json.loads(candidate)
            except ValueError:
                self.reason = "Coordinator returned an invalid decision. Use a model that follows structured instructions."
                return
            if not isinstance(decision, dict):
                self.reason = "Coordinator decision must be an object."
                return
            if decision.get("action") == "finish" and isinstance(decision.get("result"), str) and decision["result"].strip():
                self.final_result = decision["result"]
                self.ok = True
                return
            node = self.team.node(decision.get("agent"))
            assignment = decision.get("task")
            if decision.get("action") != "delegate" or node is None or not isinstance(assignment, str) or not assignment.strip():
                self.reason = "Coordinator requested an invalid or unavailable specialist."
                return
            if used >= config["maxDelegations"]:
                self.reason = "Coordinator delegation limit reached."
                return
            used += 1
            self._emit({"type": "delegation", "id": node.id, "task": assignment,
                        "count": used, "max": config["maxDelegations"]})
            task = render(node.prompt, self.outputs) + "\n\nCoordinator assignment:\n" + assignment
            result, failed = self._run_node(node, task)
            self.outputs[node.id] = result
            if failed:
                self.reason = f"Specialist {node.id} failed."
                return

    def _loop_at(self, node_id: str) -> Loop | None:
        return next((l for l in self.team.loops if l.at == node_id), None)

    def _run_node(self, node: Node, prompt: str) -> tuple[str, bool]:
        """Run one node to completion. Returns (output_text, failed)."""
        sections = []
        if node.instructions.strip():
            sections.append("Agent instructions:\n" + node.instructions.strip())
        sections.append(prompt)
        if node.include_context:
            sections.append("Original brief:\n" + self.outputs["input"])
            prior = self.team.nodes if self.team.coordinator else self.team.nodes[:self.team.index_of(node.id)]
            for previous in prior:
                if previous.id == node.id or not self.outputs.get(previous.id):
                    continue
                sections.append(f"Output from {previous.role} ({previous.id}):\n{self.outputs[previous.id]}")
        prompt = "\n\n".join(sections)
        session = self._sessions.get(node.id)
        if session is None:
            session = chat.ChatSession(None, self.team.cwd, node.engine)
            self._sessions[node.id] = session
        channel = session.subscribe()
        self._emit({"type": "node_started", "id": node.id, "role": node.role})
        session.send(prompt, node.posture, node.model)

        result, failed = "", False
        messages = []
        try:
            while True:
                event = channel.get()
                kind = event.get("type")
                # Forward the node's activity so the board can show it live.
                self._emit({"type": "node_event", "id": node.id, "event": event})
                if kind == "turn_started" and not session.session_id:
                    # Belt-and-suspenders: make the session resumable for loops.
                    session.session_id = event.get("sessionId")
                if kind == "assistant_message" and event.get("text"):
                    messages.append(event["text"])
                if kind == "turn_done":
                    result = event.get("result") or "\n\n".join(messages)
                    failed = not event.get("ok", True)
                    break
                if kind == "error":
                    failed = True
                    break
        finally:
            session.unsubscribe(channel)
        self._emit({"type": "node_done", "id": node.id, "output": result, "ok": not failed})
        return result, failed


class TeamManager:
    """Holds running team executions, keyed by team name (one run per team)."""

    def __init__(self) -> None:
        self._runs: dict[str, TeamRun] = {}
        self._lock = threading.Lock()

    def start(self, team: Team, team_input: str) -> TeamRun:
        run = TeamRun(team, team_input)
        with self._lock:
            existing = self._runs.get(team.name)
            if existing and not existing.done:
                raise ValueError("This workflow is already running. Stop it or wait for it to finish.")
            self._runs[team.name] = run
        run.start()
        return run

    def get(self, name: str) -> TeamRun | None:
        with self._lock:
            return self._runs.get(name)

    def cancel(self, name: str) -> None:
        with self._lock:
            run = self._runs.get(name)
        if run:
            run.cancel()
