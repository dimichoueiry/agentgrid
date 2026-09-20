"""Personas: the managers you own, kept in a bank and posted to teams.

An orchestrator used to be one record that said both who it was and where it
worked. That made it impossible to reuse: a good Product Manager written for
one product had to be written again for the next, and everything it learned
about how you like work done stayed stuck in the first copy.

A persona is the *who*. It is posted to a team -- a work area, a product --
and that posting is the orchestrator you open and talk to
(`orchestrator.Orchestrator`). One persona may be posted to several teams at
once. Edit the persona and every posting has the change on its next step.

What belongs to the persona, and so travels with it:

- **Guidelines** -- its role, how it thinks, what good looks like.
- **Its OpenRouter model** -- the brain it thinks with.
- **Agent defaults and allowed models** -- how the agents it starts are
  shaped. Defaults fill whatever the model leaves out; the allowed list is a
  fence the harness enforces, so "stop starting Opus 4.5 background agents"
  is said once, here, rather than to every new orchestrator.
- **Its toolkit** -- saved agents it may start as its team, skills it knows
  about, reusable prompts from the prompt library. Names only: the things
  themselves stay where they live, and a missing one is skipped, not fatal.
- **Its memory of how to work** -- lessons that apply on every team.

What a posting knows about one team -- its product, the facts it has been
told -- stays with the posting (see `orchestrator.remember`).

Like `orchestrator`, this module is inert: data, validation and files.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agentgrid import models

DIR = Path.home() / ".agentgrid" / "personas"
# Written once the starter personas have been offered, so deleting one is
# respected rather than undone on the next start.
SEEDED_MARKER = ".seeded"
_LOCK = threading.RLock()

MAX_NAME = 60
MAX_DESCRIPTION = 200
# Guidelines carry the whole of who a persona is, so they are deliberately not
# capped by a character count: a long, careful prompt must survive intact. The
# only bound is the server's request-body limit (web.MAX_BODY_BYTES, 16 MB),
# which is a transport safeguard, not an editing limit.
MAX_LIST = 40
MAX_MEMORY_TEXT = 500
MEMORY_LIMIT = 100
ENGINES = ("claude", "codex")
# The session is a rule, not a hint. "interactive" and "background" are what
# every agent the persona starts runs as, whatever the model asks for;
# "either" lets the model choose and falls back to background when it does not
# say. A setting that only suggested was read, reasonably, as a rule -- and
# then broken by a model that asked for the other kind.
AGENT_MODES = ("interactive", "background", "either")
# What "the default is Opus 5, interactive" means as data. Used for the
# starters and for personas converted from older orchestrators.
PREFERRED_AGENT_MODEL = "claude-opus-5"


@dataclass
class AgentDefaults:
    """How an agent this persona starts is shaped when the model does not say."""

    engine: str = "claude"
    model: str = ""               # "" leaves it to the CLI's own default
    mode: str = "either"          # interactive | background | either

    def to_dict(self) -> dict:
        return {"engine": self.engine, "model": self.model, "mode": self.mode}

    def locked_mode(self) -> str:
        """The session every agent must run as, or "" when the model chooses."""
        return self.mode if self.mode in ("interactive", "background") else ""


@dataclass
class Persona:
    id: str
    name: str
    model: str                    # the OpenRouter model it thinks with
    description: str = ""
    guidelines: str = ""
    agent_defaults: AgentDefaults = field(default_factory=AgentDefaults)
    allowed_models: list[str] = field(default_factory=list)   # empty: any
    team: list[str] = field(default_factory=list)             # saved agent names
    skills: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    memory: list[dict] = field(default_factory=list)          # [{id, text, at}]

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "model": self.model,
                "description": self.description, "guidelines": self.guidelines,
                "agentDefaults": self.agent_defaults.to_dict(),
                "allowedModels": list(self.allowed_models), "team": list(self.team),
                "skills": list(self.skills), "prompts": list(self.prompts),
                "memory": [dict(item) for item in self.memory]}

    def allows(self, model: str) -> bool:
        """Whether an agent may be started on `model`. An empty list allows
        everything; the comparison ignores case, as the CLIs do."""
        if not self.allowed_models:
            return True
        return str(model or "").lower() in {m.lower() for m in self.allowed_models}


# ---------------------------------------------------------------------------
# Validation. Messages are written for the field they appear under.


def _names(raw: object, label: str) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list.")
    seen, result = set(), []
    for item in raw[:MAX_LIST]:
        name = str(item or "").strip()[:80]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def _model_id(value: object, label: str) -> str:
    model = str(value or "").strip()
    if model and not models.valid(model):
        raise ValueError(f"{label}: \"{model[:60]}\" is not a model ID.")
    return model


def load_defaults(raw: object) -> AgentDefaults:
    raw = raw if isinstance(raw, dict) else {}
    engine = raw.get("engine") if raw.get("engine") in ENGINES else "claude"
    mode = raw.get("mode") if raw.get("mode") in AGENT_MODES else "either"
    return AgentDefaults(engine=engine, model=_model_id(raw.get("model"), "Default agent model"),
                         mode=mode)


def load(raw: object, *, existing_id: str = "") -> Persona:
    """A Persona from client or on-disk JSON, or ValueError saying why not."""
    if not isinstance(raw, dict):
        raise ValueError("A persona must be a JSON object.")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"Give the persona a name between 1 and {MAX_NAME} characters.")
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError("Choose the OpenRouter model this persona thinks with.")
    if len(model) > 200 or any(c.isspace() for c in model):
        raise ValueError("That is not an OpenRouter model ID.")
    description = str(raw.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"Keep the description under {MAX_DESCRIPTION} characters.")
    guidelines = str(raw.get("guidelines") or "")   # deliberately uncapped -- see the note by the constants above
    defaults = load_defaults(raw.get("agentDefaults"))
    allowed = [_model_id(m, "Allowed models") for m in _names(raw.get("allowedModels"), "Allowed models")]
    if allowed and not defaults.model:
        # With a fence and no default, "the CLI's default" could be outside
        # the fence; the first allowed model is the honest default.
        defaults.model = allowed[0]
    if allowed and defaults.model.lower() not in {m.lower() for m in allowed}:
        raise ValueError("The default agent model has to be one of the allowed models.")
    memory = raw.get("memory") if isinstance(raw.get("memory"), list) else []
    return Persona(
        id=str(raw.get("id") or existing_id or uuid.uuid4().hex),
        name=name, model=model, description=description, guidelines=guidelines,
        agent_defaults=defaults, allowed_models=allowed,
        team=_names(raw.get("team"), "Team"), skills=_names(raw.get("skills"), "Skills"),
        prompts=_names(raw.get("prompts"), "Prompts"),
        memory=[item for item in memory if isinstance(item, dict) and item.get("text")],
    )


# ---------------------------------------------------------------------------
# The store: one JSON file per persona, memory included.


def _path(persona_id: str) -> Path:
    if not persona_id or not persona_id.replace("-", "").isalnum():
        raise ValueError("Unknown persona.")
    return DIR / f"{persona_id}.json"


def _write(persona: Persona) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    path = _path(persona.id)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(persona.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def list_all() -> list[Persona]:
    """Every readable persona, by name. A corrupt file is skipped, not fatal."""
    found = []
    with _LOCK:
        try:
            paths = sorted(DIR.glob("*.json"))
        except OSError:
            return []
        for path in paths:
            try:
                found.append(load(json.loads(path.read_text("utf-8")), existing_id=path.stem))
            except (OSError, ValueError):
                continue
    return sorted(found, key=lambda p: p.name.lower())


def get(persona_id: str) -> Persona:
    with _LOCK:
        try:
            raw = json.loads(_path(persona_id).read_text("utf-8"))
        except (OSError, ValueError):
            raise ValueError("That persona no longer exists.") from None
    return load(raw, existing_id=persona_id)


def save(persona: Persona) -> Persona:
    """Store a persona's definition. Its memory is kept from disk: the editor
    changes who it is, and memory changes only through remember/forget."""
    with _LOCK:
        clash = next((p for p in list_all() if p.id != persona.id
                      and p.name.casefold() == persona.name.casefold()), None)
        if clash is not None:
            raise ValueError(f"A persona called \"{clash.name}\" already exists.")
        try:
            persona.memory = get(persona.id).memory
        except ValueError:
            pass                                  # a new persona keeps what it was given
        _write(persona)
    return persona


def delete(persona_id: str) -> None:
    """Remove a persona. The caller refuses while it is still posted anywhere."""
    with _LOCK:
        try:
            _path(persona_id).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Memory: lessons about how to work. Bounded, de-duplicated, removable.


def remember(persona_id: str, text: str) -> dict:
    text = " ".join(str(text or "").split())[:MAX_MEMORY_TEXT]
    if not text:
        raise ValueError("Say what to remember.")
    with _LOCK:
        persona = get(persona_id)
        existing = next((m for m in persona.memory if m.get("text", "").casefold() == text.casefold()), None)
        if existing:
            return existing
        item = {"id": uuid.uuid4().hex[:12], "text": text, "at": time.time()}
        persona.memory = (persona.memory + [item])[-MEMORY_LIMIT:]
        _write(persona)
    return item


def forget(persona_id: str, memory_id: str) -> bool:
    with _LOCK:
        persona = get(persona_id)
        kept = [m for m in persona.memory if m.get("id") != memory_id]
        if len(kept) == len(persona.memory):
            return False
        persona.memory = kept
        _write(persona)
    return True


# ---------------------------------------------------------------------------
# Where personas come from: converted from older orchestrators, and a small
# starter bank offered once.


def preferred_defaults() -> tuple[AgentDefaults, list[str]]:
    """Opus 5, interactive, and only Opus 5 -- the setting that makes the
    default stick rather than merely suggesting it."""
    return (AgentDefaults(engine="claude", model=PREFERRED_AGENT_MODEL, mode="interactive"),
            [PREFERRED_AGENT_MODEL])


def from_legacy(name: str, model: str, instructions: str) -> Persona:
    """A persona made from an orchestrator written before personas existed:
    its instructions were its guidelines all along."""
    defaults, allowed = preferred_defaults()
    return load({"name": name, "model": model or "openai/gpt-5", "guidelines": instructions,
                 "description": "Converted from the orchestrator of the same name.",
                 "agentDefaults": defaults.to_dict(), "allowedModels": allowed})


STARTERS = [
    {
        "name": "Product Manager",
        "description": "Turns a product goal into shipped, verified work.",
        "skills": ["writing-playbook", "engineering-playbook"],
        "guidelines": (
            "You are a product manager. You own the outcome, not the code.\n"
            "- Start from the user problem. Restate the goal in one sentence before planning.\n"
            "- Break work into tickets small enough for one agent each, with a clear 'done when'.\n"
            "- Ask the user only for decisions they must make; decide everything else.\n"
            "- Check each agent's result against its 'done when' before calling it done.\n"
            "- Report in short bullets: what shipped, what is next, what needs the user."
        ),
    },
    {
        "name": "Engineering Lead",
        "description": "Plans, delegates and reviews engineering work.",
        "skills": ["worktree-task", "engineering-playbook"],
        "guidelines": (
            "You are an engineering lead. You plan, delegate and review; agents write the code.\n"
            "- Every coding task goes in its own branch and worktree: tell agents to use the "
            "worktree-task skill.\n"
            "- Ask for tests with every change, and for the test output in the report.\n"
            "- Prefer small, reviewable changes over one large one.\n"
            "- Read the agent's output before approving its work; never assume it passed.\n"
            "- Keep the user's codebase conventions; do not introduce new frameworks unasked."
        ),
    },
]


def seed_starters(available_skills: set[str], model: str = "openai/gpt-5") -> list[str]:
    """Offer the starter personas once. Skills that are not installed are left
    off rather than listed as missing. Returns the names created."""
    with _LOCK:
        marker = DIR / SEEDED_MARKER
        if marker.exists():
            return []
        created = []
        defaults, allowed = preferred_defaults()
        taken = {p.name.casefold() for p in list_all()}
        for starter in STARTERS:
            if starter["name"].casefold() in taken:
                continue
            persona = load({**starter, "model": model, "agentDefaults": defaults.to_dict(),
                            "allowedModels": allowed,
                            "skills": [s for s in starter["skills"] if s in available_skills]})
            _write(persona)
            created.append(persona.name)
        DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    return created
