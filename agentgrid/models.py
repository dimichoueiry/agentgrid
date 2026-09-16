"""Model suggestions from the user's local CLI caches, without network calls.

Suggestions are not an entitlement check: the CLI validates the chosen model.
Custom IDs remain usable when caches are missing, stale, or incomplete.

The caches forget: `lastModelUsage` keeps one entry per project, and a model
seen only in a running session drops off when the session ages out. So a
model a session has actually answered with is remembered in
~/.agentgrid/models.json and listed from then on. Only session models are
remembered, never a model someone merely asked for: a session's model comes
from its replies, and a model the API refused never replies (the error entry
carries the placeholder `<synthetic>` instead).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

ENGINES = ("claude", "codex")
# The CLI's aliases for "the latest of this family". Each also takes a [1m]
# suffix for the 1M-token context window.
CLAUDE_ALIASES = ("fable", "opus", "sonnet", "haiku")
# What a model ID may look like. It is passed to the CLI as its own argument,
# so this is less about quoting than about refusing what is plainly not an ID:
# a flag (`--foo`), a placeholder (`<synthetic>`), a sentence.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$")
_CLAUDE_ID = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-(\d{8}))?$")
_LOCK = threading.Lock()


def valid(model: str) -> bool:
    return isinstance(model, str) and bool(MODEL_RE.match(model))


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _parts(model: str) -> tuple[str, bool]:
    """The ID without a [1m] suffix, and whether it had one."""
    return (model[:-4], True) if model.endswith("[1m]") else (model, False)


def label(engine: str, model: str) -> str:
    """A readable name: claude-opus-5 -> Claude Opus 5. Anything else as is."""
    if engine != "claude":
        return model
    base, wide = _parts(model)
    extras = ["1M context"] if wide else []
    if base in CLAUDE_ALIASES:
        return f"{base.title()} ({', '.join(['alias'] + extras)})"
    found = _CLAUDE_ID.match(base)
    if not found:
        return model
    family, major, minor, day = found.groups()
    if day:
        extras.insert(0, f"{day[:4]}-{day[4:6]}-{day[6:]}")
    name = f"Claude {family.title()} {major}{'.' + minor if minor else ''}"
    return f"{name} ({', '.join(extras)})" if extras else name


def _newest_first(model: str) -> tuple:
    found = _CLAUDE_ID.match(_parts(model)[0])
    if not found:
        return (1, 0, 0, "", model)
    family, major, minor, day = found.groups()
    return (0, -int(major), -int(minor or 0), family, day or "", model)


def remembered_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".agentgrid" / "models.json"


def _saved(path: Path) -> dict[str, list[str]]:
    data = _read(path)
    return {e: [m for m in data.get(e) or [] if valid(m)] for e in ENGINES}


def _remember(path: Path, seen: dict[str, list[str]]) -> None:
    with _LOCK:
        merged = _saved(path)
        new = {e: [m for m in seen[e] if m not in merged[e]] for e in ENGINES}
        if not any(new.values()):
            return
        for engine in ENGINES:
            merged[engine] += new[engine]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            pass    # a suggestion list that can't be saved is still a suggestion list


def catalog(sessions: list[dict] | tuple = (), home: Path | None = None) -> dict:
    home = home or Path.home()
    claude: list[str] = []
    codex: list[list[str]] = []
    hidden: set[str] = set()

    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    cached = _read(codex_home / "models_cache.json").get("models", [])
    for model in cached if isinstance(cached, list) else []:
        if not isinstance(model, dict) or not valid(model.get("slug")):
            continue
        if model.get("visibility", "list") != "list":
            hidden.add(model["slug"])
        elif all(row[0] != model["slug"] for row in codex):
            name = model.get("display_name")
            codex.append([model["slug"], name if isinstance(name, str) and name else model["slug"]])

    config = _read(home / ".claude.json")
    claude.append(_read(home / ".claude" / "settings.json").get("model"))
    # Claude Code's own /model picker rows beyond the aliases.
    options = config.get("additionalModelOptionsCache")
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict):
            claude.append(option.get("value"))
    projects = config.get("projects", {})
    for project in projects.values() if isinstance(projects, dict) else []:
        usage = project.get("lastModelUsage", {}) if isinstance(project, dict) else {}
        if isinstance(usage, dict):
            claude.extend(k for k in usage if k.startswith("claude-"))

    path = remembered_path(home)
    saved = _saved(path)
    seen: dict[str, list[str]] = {e: [] for e in ENGINES}
    for session in sessions:
        engine, model = session.get("engine") or "claude", session.get("model")
        # A codex session with no recorded model reports the engine's name.
        if (engine in ENGINES and valid(model) and model != engine and model not in hidden
                and model not in seen[engine]):
            seen[engine].append(model)
    _remember(path, seen)

    for model in saved["codex"] + seen["codex"]:
        if model not in hidden and all(row[0] != model for row in codex):
            codex.append([model, model])
    claude = [m for m in dict.fromkeys(claude + saved["claude"] + seen["claude"]) if valid(m)]
    # An alias the user has set (fable[1m]) sits with the plain ones.
    aliases = list(CLAUDE_ALIASES) + [m for m in claude if _parts(m)[0] in CLAUDE_ALIASES
                                      and m not in CLAUDE_ALIASES]
    specific = sorted((m for m in claude if _parts(m)[0] not in CLAUDE_ALIASES), key=_newest_first)
    return {
        "claude": [["", "Default"]] + [[m, label("claude", m)] for m in aliases + specific],
        "codex": [["", "Default"]] + codex,
    }


def listed(engine: str, model: str, home: Path | None = None) -> bool:
    """Whether a model is one this machine has a record of for that engine."""
    return any(row[0] == model for row in catalog(home=home).get(engine, []))
