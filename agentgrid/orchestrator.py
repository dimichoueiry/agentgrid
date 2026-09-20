"""Orchestrators: a persona posted to a team, deciding which agents to start.

An orchestrator is not a session. It is a *posting* -- one persona from the
bank (`personas`) assigned to a work area and a product -- plus a run state on
disk, driven by the persona's OpenRouter model, whose only powers are the
tools AgentGrid hands it: starting agents, reading what they produced, filing
tickets, talking to you.

Who it is lives on the persona: guidelines, model, agent defaults, toolkit,
lessons about how to work. Where it works lives here: the team (scope), the
project, a product brief, the facts it has been told about this team, and its
runs. Moving a manager to another team is posting the same persona again. It never touches the filesystem or a shell itself; the agents it starts
are ordinary Claude Code or Codex sessions on this machine, launched through
exactly the same boundary as the New agent sheet.

Three ideas hold the design together:

- **Scope.** ``scope`` is a work area id, or ``""`` for the global
  orchestrator that sees every area. An area may have as many as you like; they
  are keyed by id, not by name.
- **Durability.** Everything an orchestrator knows lives in
  ``~/.agentgrid/orchestrators/<id>/``: the definition, the run state (the
  model conversation, counters, children, a pending approval) and an
  append-only journal. State is written after every step, so closing the lid
  interrupts a run rather than losing it -- on the next start it resumes from
  the same conversation.
- **The harness holds the limits.** Mode (`ask`/`auto`), the spawn caps and
  the spend ceiling are enforced in `orchestrator_run`, never asked of the
  model. A model that ignores its instructions still cannot exceed them.

This module is deliberately inert: data, validation and files. No network, no
threads, no subprocesses. That keeps it testable on its own and keeps the run
engine honest about where side effects are allowed to happen.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DIR = Path.home() / ".agentgrid" / "orchestrators"
_LOCK = threading.RLock()

MODES = ("ask", "auto")
# A run's status is the whole of what the board needs to draw it, so it maps
# onto columns the user already has: `waiting` is "Needs you" (an approval is
# pending), `running` is working, the rest are at rest.
STATUSES = ("idle", "running", "waiting", "done", "failed", "stopped")
ACTIVE_STATUSES = ("running", "waiting")

MAX_NAME = 60
# Legacy "instructions" are read only so an old orchestrator can be converted
# into a persona, where they become its guidelines. Like persona guidelines,
# they are kept whole -- the request body limit is the only bound -- so a long
# pre-personas prompt is never lost on conversion.
MAX_BRIEF = 20_000
MAX_GOAL = 20_000
MAX_MEMORY_TEXT = 500
MEMORY_LIMIT = 100
# The journal is read tail-first for the Activity tab; the cap stops a
# long-lived orchestrator from turning into an unbounded file.
JOURNAL_LIMIT = 500
MAX_JOURNAL_BYTES = 8 * 1024 * 1024


@dataclass
class Limits:
    """The envelope a run may not leave, whatever the model asks for."""

    max_concurrent: int = 3       # children running at once
    max_spawns: int = 20          # children started in one run
    max_spend_usd: float = 5.0    # OpenRouter cost of the orchestrator's own thinking
    max_steps: int = 200          # model calls in one run

    def to_dict(self) -> dict:
        return {"maxConcurrent": self.max_concurrent, "maxSpawns": self.max_spawns,
                "maxSpendUsd": self.max_spend_usd, "maxSteps": self.max_steps}


@dataclass
class Orchestrator:
    """A posting: which persona, on which team, with what brief and budget.

    Trust (`mode`) and budget (`limits`) are the posting's, not the
    persona's: how much rope a manager gets is a decision about one team.
    `model` and `instructions` are only ever read from files written before
    personas existed, and are emptied when those are converted.
    """

    id: str
    name: str
    persona_id: str = ""
    scope: str = ""               # work area id, or "" for every area
    brief: str = ""               # what this team / product is
    mode: str = "ask"
    cwd: str = ""                 # the project it starts from, if it has one
    limits: Limits = field(default_factory=Limits)
    model: str = ""               # legacy: now the persona's
    instructions: str = ""        # legacy: now the persona's guidelines

    def to_dict(self) -> dict:
        record = {"id": self.id, "name": self.name, "personaId": self.persona_id,
                  "scope": self.scope, "brief": self.brief, "mode": self.mode, "cwd": self.cwd,
                  "limits": self.limits.to_dict()}
        if self.model or self.instructions:
            record.update(model=self.model, instructions=self.instructions)
        return record


# ---------------------------------------------------------------------------
# Validation. Every message is one a user can act on, because these are the
# strings the sheet shows under the field that is wrong.


def _clamp_int(value, low: int, high: int, default: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.") from None
    if not low <= number <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return number


def load_limits(raw: object) -> Limits:
    raw = raw if isinstance(raw, dict) else {}
    try:
        spend = float(raw.get("maxSpendUsd", Limits.max_spend_usd) or 0)
    except (TypeError, ValueError):
        raise ValueError("Spend limit must be a number of dollars.") from None
    if not 0 <= spend <= 1000:
        raise ValueError("Spend limit must be between $0 and $1000.")
    return Limits(
        max_concurrent=_clamp_int(raw.get("maxConcurrent"), 1, 10, Limits.max_concurrent, "Agents at once"),
        max_spawns=_clamp_int(raw.get("maxSpawns"), 1, 200, Limits.max_spawns, "Agent limit"),
        max_spend_usd=round(spend, 4),
        max_steps=_clamp_int(raw.get("maxSteps"), 1, 1000, Limits.max_steps, "Step limit"),
    )


def load(raw: object, *, existing_id: str = "") -> Orchestrator:
    """An Orchestrator from client or on-disk JSON, or ValueError saying why not."""
    if not isinstance(raw, dict):
        raise ValueError("An orchestrator must be a JSON object.")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"Give it a name between 1 and {MAX_NAME} characters.")
    persona_id = str(raw.get("personaId") or "").strip()
    # A file from before personas carries its own model instead; it is read
    # so it can be converted, never written in that shape again.
    model = str(raw.get("model") or "").strip() if not persona_id else ""
    if not persona_id and not model:
        raise ValueError("Choose a persona for this orchestrator.")
    if model and (len(model) > 200 or any(c.isspace() for c in model)):
        raise ValueError("That is not an OpenRouter model ID.")
    instructions = str(raw.get("instructions") or "") if not persona_id else ""
    brief = str(raw.get("brief") or "")
    if len(brief) > MAX_BRIEF:
        raise ValueError(f"Keep the product brief under {MAX_BRIEF:,} characters.")
    mode = raw.get("mode") if raw.get("mode") in MODES else "ask"
    return Orchestrator(
        id=str(raw.get("id") or existing_id or uuid.uuid4().hex),
        name=name, persona_id=persona_id, scope=str(raw.get("scope") or ""), brief=brief,
        mode=mode, cwd=str(raw.get("cwd") or ""), limits=load_limits(raw.get("limits")),
        model=model, instructions=instructions,
    )


# ---------------------------------------------------------------------------
# The store: one directory per orchestrator, so its definition, state and
# journal are removed together and nothing survives a delete.


def _home(orchestrator_id: str) -> Path:
    if not orchestrator_id or not orchestrator_id.replace("-", "").isalnum():
        raise ValueError("Unknown orchestrator.")
    return DIR / orchestrator_id


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def list_all() -> list[Orchestrator]:
    """Every readable orchestrator. A corrupt one is skipped, not fatal."""
    found = []
    with _LOCK:
        try:
            homes = sorted(p for p in DIR.iterdir() if p.is_dir())
        except OSError:
            return []
        for home in homes:
            try:
                found.append(load(json.loads((home / "definition.json").read_text("utf-8")),
                                  existing_id=home.name))
            except (OSError, ValueError):
                continue
    return found


def get(orchestrator_id: str) -> Orchestrator:
    with _LOCK:
        try:
            raw = json.loads((_home(orchestrator_id) / "definition.json").read_text("utf-8"))
        except (OSError, ValueError):
            raise ValueError("That orchestrator no longer exists.") from None
    return load(raw, existing_id=orchestrator_id)


def save(orchestrator: Orchestrator) -> Orchestrator:
    """Store a definition. Names are unique within a scope, so the board and
    an agent's `AGENTGRID_AGENT` label stay unambiguous."""
    with _LOCK:
        clash = next((o for o in list_all() if o.id != orchestrator.id
                      and o.scope == orchestrator.scope
                      and o.name.casefold() == orchestrator.name.casefold()), None)
        if clash is not None:
            where = "this work area" if orchestrator.scope else "the global scope"
            raise ValueError(f"An orchestrator called \"{clash.name}\" already exists in {where}.")
        _write_json(_home(orchestrator.id) / "definition.json", orchestrator.to_dict())
    return orchestrator


def delete(orchestrator_id: str) -> None:
    """Remove a definition with its state and journal. A running one must be
    stopped first -- that is the caller's job, since only it holds the runs."""
    with _LOCK:
        home = _home(orchestrator_id)
        for name in ("definition.json", "state.json", "journal.jsonl", "memory.json"):
            try:
                (home / name).unlink()
            except OSError:
                pass
        try:
            home.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Run state. One JSON file, rewritten after every step, holding everything a
# resume needs: the model conversation, the counters the limits are checked
# against, the children started so far, and any approval waiting on the user.


def blank_state() -> dict:
    return {"status": "idle", "goal": "", "messages": [], "steps": 0, "spawns": 0,
            "costUsd": 0.0, "children": [], "pending": None, "plan": [], "inbox": [],
            "reason": "", "startedAt": 0.0, "updatedAt": 0.0, "lastText": "",
            "previous": None}


def read_state(orchestrator_id: str) -> dict:
    with _LOCK:
        try:
            raw = json.loads((_home(orchestrator_id) / "state.json").read_text("utf-8"))
        except (OSError, ValueError):
            return blank_state()
    if not isinstance(raw, dict):
        return blank_state()
    state = blank_state()
    state.update({k: v for k, v in raw.items() if k in state})
    if state["status"] not in STATUSES:
        state["status"] = "idle"
    return state


def write_state(orchestrator_id: str, state: dict) -> dict:
    state["updatedAt"] = time.time()
    with _LOCK:
        _write_json(_home(orchestrator_id) / "state.json", state)
    return state


def interrupted(state: dict) -> bool:
    """True for a state left mid-step by a stopped server -- what a resume looks
    for. A run waiting on an approval is *not* interrupted: it is exactly where
    it should be until the user answers."""
    return state.get("status") == "running"


# ---------------------------------------------------------------------------
# Posting memory: facts about this team -- "the SEO agent is retired", "the
# domain is mlguerrilla.com". Kept beside the run state but in its own file,
# because a new run starts a fresh conversation and must not start from zero.


def memory(orchestrator_id: str) -> list[dict]:
    with _LOCK:
        try:
            raw = json.loads((_home(orchestrator_id) / "memory.json").read_text("utf-8"))
        except (OSError, ValueError):
            return []
    return [m for m in raw if isinstance(m, dict) and m.get("text")] if isinstance(raw, list) else []


def remember(orchestrator_id: str, text: str) -> dict:
    text = " ".join(str(text or "").split())[:MAX_MEMORY_TEXT]
    if not text:
        raise ValueError("Say what to remember.")
    with _LOCK:
        items = memory(orchestrator_id)
        existing = next((m for m in items if m["text"].casefold() == text.casefold()), None)
        if existing:
            return existing
        item = {"id": uuid.uuid4().hex[:12], "text": text, "at": time.time()}
        _write_json(_home(orchestrator_id) / "memory.json", (items + [item])[-MEMORY_LIMIT:])
    return item


def forget(orchestrator_id: str, memory_id: str) -> bool:
    with _LOCK:
        items = memory(orchestrator_id)
        kept = [m for m in items if m.get("id") != memory_id]
        if len(kept) == len(items):
            return False
        _write_json(_home(orchestrator_id) / "memory.json", kept)
    return True


def postings_of(persona_id: str) -> list[Orchestrator]:
    return [o for o in list_all() if o.persona_id == persona_id]


def migrate_legacy() -> list[str]:
    """Turn every orchestrator written before personas into persona + posting.

    Its model and instructions become a persona of the same name; the
    orchestrator keeps its id, runs, journal and memory, and points at it.
    Idempotent: a converted orchestrator has a persona and is left alone.
    Returns the names converted.
    """
    from agentgrid import personas     # personas does not import this module
    converted = []
    with _LOCK:
        for posting in list_all():
            if posting.persona_id or not posting.model:
                continue
            existing = next((p for p in personas.list_all()
                             if p.name.casefold() == posting.name.casefold()), None)
            persona = existing or personas.save(
                personas.from_legacy(posting.name, posting.model, posting.instructions))
            posting.persona_id, posting.model, posting.instructions = persona.id, "", ""
            _write_json(_home(posting.id) / "definition.json", posting.to_dict())
            converted.append(posting.name)
    return converted


# ---------------------------------------------------------------------------
# The journal: an append-only record of what the orchestrator did, which is
# what the Activity tab shows and the only place a finished run is explained.


def append_journal(orchestrator_id: str, event: dict) -> dict:
    event = {"at": time.time(), **event}
    with _LOCK:
        path = _home(orchestrator_id) / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size > MAX_JOURNAL_BYTES:
                # Keep the tail rather than the head: recent activity is what
                # anyone reads, and an unbounded file is its own bug.
                tail = path.read_text("utf-8", errors="replace").splitlines()[-JOURNAL_LIMIT:]
                path.write_text("\n".join(tail) + "\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            # A journal that cannot be written must not take the run down.
            pass
    return event


def journal(orchestrator_id: str, limit: int = JOURNAL_LIMIT) -> list[dict]:
    with _LOCK:
        try:
            lines = (_home(orchestrator_id) / "journal.jsonl").read_text("utf-8", errors="replace").splitlines()
        except OSError:
            return []
    events = []
    for line in lines[-max(1, limit):]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
