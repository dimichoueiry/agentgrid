"""Model suggestions from the user's local CLI caches, without network calls.

Suggestions are not an entitlement check: the CLI validates the chosen model.
Custom IDs remain usable when caches are missing, stale, or incomplete.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog(sessions: list[dict] | tuple = (), home: Path | None = None) -> dict:
    home = home or Path.home()
    result = {
        "claude": [["", "Default"], ["sonnet", "Sonnet (alias)"],
                   ["opus", "Opus (alias)"], ["haiku", "Haiku (alias)"]],
        "codex": [["", "Default"]],
    }

    def add(engine, model, label=None):
        if (engine in result and isinstance(model, str) and model.strip()
                and not any(row[0] == model for row in result[engine])):
            result[engine].append([model, label if isinstance(label, str) and label else model])

    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    cached = _read(codex_home / "models_cache.json").get("models", [])
    for model in cached if isinstance(cached, list) else []:
        if isinstance(model, dict) and model.get("visibility", "list") == "list":
            add("codex", model.get("slug"), model.get("display_name"))
    add("claude", _read(home / ".claude" / "settings.json").get("model"))
    projects = _read(home / ".claude.json").get("projects", {})
    if isinstance(projects, dict):
        observed = set()
        for project in projects.values():
            usage = project.get("lastModelUsage", {}) if isinstance(project, dict) else {}
            if isinstance(usage, dict):
                observed.update(k for k in usage if k.startswith("claude-"))
        for model in sorted(observed):
            add("claude", model)
    for session in sessions:
        add(session.get("engine", "claude"), session.get("model"))
    return result
