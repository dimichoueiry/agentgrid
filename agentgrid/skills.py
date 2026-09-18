"""Read-only view of the Claude Code skills installed on this machine.

A skill is a folder holding a SKILL.md whose YAML front matter names it and
says what it is for. Claude Code loads them into the agents it runs; an
orchestrator cannot run one itself (it has no shell), but it can know what
each is for and tell the agents it starts which to use. This module is how it
finds out: names and descriptions for the menu, the full text on request.

Two places are read: ``~/.claude/skills/<name>/`` and the synced buckets under
``~/.claude/skills/synced/<bucket>/<name>/``. Nothing here ever writes to
either -- they belong to Claude Code and to the user.

No YAML library is available (standard library only), so the front matter is
read with a small parser that understands the two shapes skills actually use:
``key: value`` and a folded or literal block (``key: >-`` then indented lines).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path.home() / ".claude" / "skills"
MAX_DESCRIPTION = 400
MAX_BODY = 20_000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def front_matter(text: str) -> dict:
    """The ``key: value`` pairs between the opening ``---`` lines."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict = {}
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", lines[index])
        index += 1
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in (">", ">-", "|", "|-", ""):
            block = []
            while index < len(lines) and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
                if lines[index].strip() == "---":
                    break
                block.append(lines[index].strip())
                index += 1
            joiner = "\n" if value.startswith("|") else " "
            value = joiner.join(part for part in block if part).strip()
        fields[key] = value.strip("\"'")
    return fields


def _folders() -> list[Path]:
    """Skill folders, the user's own before the synced ones."""
    folders = []
    try:
        folders += sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name != "synced")
    except OSError:
        return []
    try:
        for bucket in sorted((ROOT / "synced").iterdir()):
            if bucket.is_dir() and not bucket.name.startswith("."):
                folders += sorted(p for p in bucket.iterdir() if p.is_dir())
    except OSError:
        pass
    return folders


def list_skills() -> list[dict]:
    """``[{"name", "description"}]``, one per name -- a local skill hides a
    synced one of the same name, the way a local file shadows a default."""
    found: dict[str, dict] = {}
    for folder in _folders():
        try:
            text = (folder / "SKILL.md").read_text("utf-8", errors="replace")
        except OSError:
            continue
        fields = front_matter(text)
        name = str(fields.get("name") or folder.name).strip()
        if not _NAME.match(name) or name in found:
            continue
        found[name] = {"name": name,
                       "description": str(fields.get("description") or "")[:MAX_DESCRIPTION],
                       "path": str(folder / "SKILL.md")}
    return sorted(found.values(), key=lambda s: s["name"].lower())


def read_skill(name: str) -> str:
    """The full SKILL.md for `name`, or ValueError. Looked up in the catalog,
    never built from the name, so a request cannot become a path."""
    skill = next((s for s in list_skills() if s["name"] == name), None)
    if skill is None:
        raise ValueError(f"No skill called {str(name)[:60]!r} is installed.")
    try:
        return Path(skill["path"]).read_text("utf-8", errors="replace")[:MAX_BODY]
    except OSError:
        raise ValueError(f"Could not read the {name} skill.") from None
