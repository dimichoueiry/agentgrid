"""Chat history: every conversation on disk, not only the ones still running.

The board answers "what is running now". It is fed by `claude agents`, plus a
24-hour window of ended interactive sessions read back from disk
(`discovery.recover_ended_interactive`). Both are deliberately narrow, and the
consequence was that a conversation became unreachable the day after it
ended -- the transcript was still sitting in ~/.claude/projects, and
`claude -p --resume <id>` would still have worked on it, but nothing in the UI
could find it. Over half of a working month's conversations were stranded that
way.

This module is the other door. It reads the transcript store directly, so age
and liveness stop deciding what you can open, and it owns deletion, which the
board never had: the board's `x` only hides a card (`discovery.dismiss_session`).

Two costs shape the design:

- **Scanning must stay cheap.** A listing is a glob and a `stat` per file and
  nothing more. Titles and previews need the file's contents, so they are read
  only for the page actually being shown.
- **Deleting must be survivable.** A transcript is Claude Code's own record,
  not AgentGrid's copy of one: removing it takes the conversation away from
  `claude --resume` everywhere, and nothing else has a backup. So a delete
  moves the file to a trash folder and keeps everything it detached, and only
  a purge after `TRASH_TTL` is final.

Like `personas`, this module is inert: files, metadata and validation. The
caller decides whether a conversation is live and may be deleted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentgrid import discovery

TRASH_DIR = Path.home() / ".agentgrid" / "trash"
# Long enough that a delete regretted the next morning is still recoverable,
# short enough that the trash is not a second archive.
TRASH_TTL = 7 * 24 * 3600
PAGE_SIZE = 50
# A preview is a hint, not the message; the row has one line to spend.
PREVIEW = 160

# Every per-session store a conversation leaves entries in. Each is a JSON
# object keyed by session id, so detaching one is the same operation in all of
# them -- and keeping the removed values is what makes an undo possible.
SIDECAR_PATHS = {
    "names": discovery.NAMES_PATH,
    "overrides": discovery.OVERRIDES_PATH,
    "tags": discovery.TAGS_PATH,
    "read": discovery.READ_PATH,
    "dismissed": discovery.DISMISSED_PATH,
    "systemPrompts": discovery.SYSTEM_PROMPTS_PATH,
    "hookState": discovery.HOOK_STATE_PATH,
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# Turn counts, keyed by path -> (size, mtime, turns). A transcript only ever
# grows, but re-counting a 180 MB file on every listing would make the page
# cost scale with the archive instead of with the page.
_TURNS: dict[str, tuple[int, float, int]] = {}
# The cwd a transcript actually recorded, once it has been parsed. The slug in
# the directory name is ambiguous (a "-" in a folder name encodes like a
# separator), so a real answer replaces it as soon as one is known.
_CWD: dict[str, str] = {}
# One parse cache for the whole module. `TranscriptCache` resumes from a byte
# offset, so sharing it is what makes a second listing cheap -- a fresh cache
# per request would re-read every transcript from zero.
_CACHE = discovery.TranscriptCache()


@dataclass
class Conversation:
    """One transcript on disk. `title`/`preview`/`turns` stay empty until the
    row is on a page someone is actually looking at."""

    session_id: str
    path: Path
    cwd: str
    size: int
    modified: float
    title: str = ""
    preview: str = ""
    model: str = ""
    turns: int = 0
    started: float = 0.0
    kind: str = ""
    live: bool = False

    def to_dict(self) -> dict:
        return {"sessionId": self.session_id, "cwd": self.cwd, "project": project_name(self.cwd),
                "size": self.size, "modified": self.modified, "title": self.title,
                "preview": self.preview, "model": self.model, "turns": self.turns,
                "started": self.started, "kind": self.kind, "live": self.live}


def project_name(cwd: str) -> str:
    """The last path segment, which is what a project is called in conversation."""
    return Path(cwd).name if cwd else ""


def cwd_from_slug(slug: str) -> str:
    """Undo `discovery.project_slug`: "-Users-me-Repo" -> "/Users/me/Repo".

    Lossy by nature -- a directory with a literal "-" in its name encodes the
    same way as a separator -- so the transcript's own recorded cwd wins
    whenever the file has been read. This is the fallback for a row that has
    not been parsed yet, and it is right for ordinary paths.
    """
    return slug.replace("-", "/") if slug.startswith("-") else slug


def safe_id(session_id: str) -> str:
    """A session id that cannot escape its directory, or "" if it could.

    The character class alone is not enough: "." and ".." are built entirely
    from allowed characters and are exactly the two names that would walk out
    of the trash folder when joined to it.
    """
    session_id = str(session_id or "").strip()
    if not session_id or session_id in (".", ".."):
        return ""
    return session_id if _SAFE_ID.match(session_id) else ""


# ---------------------------------------------------------------------------
# Reading the store.


def scan() -> list[Conversation]:
    """Every transcript on disk, newest first. A `stat` each, nothing parsed.

    Deliberately does not open the files: this runs on every listing, and the
    store is hundreds of megabytes. What a row needs beyond this -- its title
    and a preview -- is filled in by `describe` for one page at a time.
    """
    root = discovery.PROJECTS_DIR
    if not root.is_dir():
        return []
    found: list[Conversation] = []
    try:
        paths = list(root.glob("*/*.jsonl"))
    except OSError:
        return []
    for path in paths:
        session_id = safe_id(path.stem)
        if not session_id:
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        found.append(Conversation(session_id=session_id, path=path,
                                  cwd=_CWD.get(str(path)) or cwd_from_slug(path.parent.name),
                                  size=info.st_size, modified=info.st_mtime))
    found.sort(key=lambda c: c.modified, reverse=True)
    return found


def describe(conversations: list[Conversation], cache: discovery.TranscriptCache | None = None) -> None:
    """Fill in title, preview and turn count, in place.

    Parsing is incremental (`TranscriptCache` resumes from a byte offset), so
    the cost is paid once per conversation and then only for whatever was
    appended since. Called for a page, never for the whole store.
    """
    cache = cache or _CACHE
    for item in conversations:
        try:
            state = cache.parse(item.path)
        except OSError:
            continue
        item.title = state.get("title") or ""
        item.preview = (state.get("last_prompt") or "")[:PREVIEW]
        item.model = state.get("model") or ""
        item.started = state.get("started") or 0.0
        item.kind = state.get("session_kind") or "interactive"
        # The transcript's own cwd is authoritative; the slug is only a guess.
        item.cwd = state.get("cwd") or item.cwd
        _CWD[str(item.path)] = item.cwd
        item.turns = count_turns(item.path)


def count_turns(path: Path) -> int:
    """How many entries the transcript holds, counted by newline.

    A byte scan rather than a JSON parse: the number is for a row's "428
    messages", which does not justify decoding tens of megabytes. The answer
    is cached against the file's size and mtime, because the store holds
    transcripts of a hundred megabytes and a listing must not re-read them.
    """
    key = str(path)
    try:
        info = path.stat()
    except OSError:
        return 0
    cached = _TURNS.get(key)
    if cached is not None and cached[0] == info.st_size and cached[1] == info.st_mtime:
        return cached[2]
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                total += chunk.count(b"\n")
    except OSError:
        return 0
    _TURNS[key] = (info.st_size, info.st_mtime, total)
    return total


def find(session_id: str) -> Path | None:
    """The transcript for a session id, resolved only against what is on disk.

    The id arrives from a client, so it is never joined into a path. It is
    matched against the scan instead, which can only ever yield files that are
    really in the store.
    """
    session_id = safe_id(session_id)
    if not session_id:
        return None
    for item in scan():
        if item.session_id == session_id:
            return item.path
    return None


def page(offset: int = 0, limit: int = PAGE_SIZE, search: str = "", project: str = "",
         cache: discovery.TranscriptCache | None = None) -> dict:
    """One page of history, filtered and described.

    A search has to look at titles and previews, which means parsing; it is
    applied after `describe` so a match on a conversation's opening line works,
    and the parse is bounded by `SEARCH_SCAN` conversations so a typo cannot
    walk the whole store.
    """
    items = scan()
    if project:
        items = [c for c in items if project_name(c.cwd).casefold() == project.casefold()
                 or c.cwd.casefold() == project.casefold()]
    if search:
        needle = search.casefold()
        described = items[:SEARCH_SCAN]
        describe(described, cache)
        items = [c for c in described
                 if needle in (c.title or "").casefold()
                 or needle in (c.preview or "").casefold()
                 or needle in c.cwd.casefold()]
    total = len(items)
    window = items[max(0, offset):max(0, offset) + max(1, limit)]
    describe(window, cache)
    return {"total": total, "offset": max(0, offset), "limit": max(1, limit),
            "conversations": [c.to_dict() for c in window]}


# How far a search will read before it stops. Titles live inside the files, so
# searching means parsing; this keeps a single keystroke from touching every
# transcript ever written.
SEARCH_SCAN = 400


def warm(cache: discovery.TranscriptCache | None = None) -> int:
    """Parse every transcript once, so listings and the project filter are
    accurate and instant afterwards.

    Run off the request path (a thread at start-up). Until it finishes, a row
    still lists -- it just shows the slug-derived project and no title, which
    is why nothing here is required for the view to work.
    """
    items = scan()
    describe(items, cache)
    return len(items)


def projects() -> list[dict]:
    """The projects history covers, with a count each, for the filter.

    Grouped on the cwd each transcript recorded where that is known, so two
    spellings of the same directory do not become two filters. Falls back to
    the directory slug for anything not parsed yet.
    """
    counts: dict[str, int] = {}
    for item in scan():
        counts[item.cwd] = counts.get(item.cwd, 0) + 1
    return sorted(({"cwd": cwd, "name": project_name(cwd) or cwd, "count": n}
                   for cwd, n in counts.items()),
                  key=lambda entry: (-entry["count"], entry["name"].casefold()))


# ---------------------------------------------------------------------------
# Deleting, reversibly.


def _trash_home(session_id: str) -> Path:
    return TRASH_DIR / session_id


def _detach_sidecars(session_id: str) -> dict:
    """Remove this session's entry from every per-session store.

    Returns what was removed, so a restore can put it back exactly. A store
    that never mentioned the session contributes nothing.
    """
    removed: dict = {}
    for label, path in SIDECAR_PATHS.items():
        store = discovery._read_json(path)
        if session_id in store:
            removed[label] = store.pop(session_id)
            discovery._write_json(path, store)
    return removed


def _restore_sidecars(session_id: str, removed: dict) -> None:
    """Put back exactly what `_detach_sidecars` took, under the same key."""
    for label, value in (removed or {}).items():
        path = SIDECAR_PATHS.get(label)
        if path is None:
            continue
        store = discovery._read_json(path)
        store[session_id] = value
        discovery._write_json(path, store)


def delete(session_id: str) -> dict:
    """Move a conversation to the trash and detach everything that pointed at it.

    Not an unlink: the transcript is Claude Code's own record and nothing else
    keeps a copy, so this is reversible until `purge_expired` comes for it.
    The caller refuses first if the session is still running -- taking the
    transcript out from under a live turn would corrupt it.
    """
    session_id = safe_id(session_id)
    if not session_id:
        raise ValueError("Unknown conversation.")
    path = find(session_id)
    if path is None:
        raise ValueError("That conversation no longer exists.")
    home = _trash_home(session_id)
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    removed = _detach_sidecars(session_id)
    queue = _detach_queue(session_id, home)
    target = home / "transcript.jsonl"
    try:
        os.replace(path, target)
    except OSError:
        # A different filesystem, or a file that moved under us: copy, then
        # remove, so a failure leaves the original where it was.
        shutil.copy2(path, target)
        try:
            path.unlink()
        except OSError:
            pass
    manifest = {"sessionId": session_id, "origin": str(path), "deletedAt": time.time(),
                "size": target.stat().st_size if target.exists() else 0,
                "sidecars": removed, "queue": queue}
    (home / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return manifest


def _detach_queue(session_id: str, home: Path) -> bool:
    """Take a session's queued chat messages with it. True if there were any."""
    try:
        from agentgrid import chat
    except Exception:  # noqa: BLE001 -- history must not depend on chat importing
        return False
    try:
        source = chat.queue_path(session_id)
    except Exception:  # noqa: BLE001
        return False
    if not source.exists():
        return False
    try:
        shutil.move(str(source), str(home / "queue.json"))
    except OSError:
        return False
    return True


def restore(session_id: str) -> bool:
    """Undo a delete: the transcript goes back, and so does everything it had."""
    session_id = safe_id(session_id)
    if not session_id:
        return False
    home = _trash_home(session_id)
    try:
        manifest = json.loads((home / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    source = home / "transcript.jsonl"
    origin = Path(str(manifest.get("origin") or ""))
    if not source.exists() or not origin.name:
        return False
    try:
        origin.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, origin)
    except OSError:
        try:
            shutil.copy2(source, origin)
        except OSError:
            return False
    _restore_sidecars(session_id, manifest.get("sidecars") or {})
    if manifest.get("queue"):
        _restore_queue(session_id, home)
    shutil.rmtree(home, ignore_errors=True)
    return True


def _restore_queue(session_id: str, home: Path) -> None:
    saved = home / "queue.json"
    if not saved.exists():
        return
    try:
        from agentgrid import chat
        target = chat.queue_path(session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(saved), str(target))
    except Exception:  # noqa: BLE001 -- a lost queue must not fail the restore
        pass


def trashed() -> list[dict]:
    """What is in the trash, newest first, with how long it has left."""
    if not TRASH_DIR.is_dir():
        return []
    items = []
    now = time.time()
    for home in TRASH_DIR.iterdir():
        if not home.is_dir():
            continue
        try:
            manifest = json.loads((home / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        deleted_at = float(manifest.get("deletedAt") or 0)
        items.append({"sessionId": manifest.get("sessionId") or home.name,
                      "origin": manifest.get("origin") or "",
                      "deletedAt": deleted_at,
                      "size": int(manifest.get("size") or 0),
                      "expiresIn": max(0.0, deleted_at + TRASH_TTL - now)})
    items.sort(key=lambda item: item["deletedAt"], reverse=True)
    return items


def purge_expired(now: float | None = None) -> list[str]:
    """Delete for real whatever has sat in the trash past `TRASH_TTL`.

    Runs on start and on each listing. Returns the ids it removed.
    """
    if not TRASH_DIR.is_dir():
        return []
    now = time.time() if now is None else now
    gone = []
    for home in list(TRASH_DIR.iterdir()):
        if not home.is_dir():
            continue
        try:
            manifest = json.loads((home / "manifest.json").read_text("utf-8"))
            deleted_at = float(manifest.get("deletedAt") or 0)
        except (OSError, ValueError):
            # A trash entry we cannot read has no expiry we can trust; leave
            # it rather than destroy something on a bad parse.
            continue
        if now - deleted_at > TRASH_TTL:
            shutil.rmtree(home, ignore_errors=True)
            gone.append(manifest.get("sessionId") or home.name)
    return gone


def purge_now(session_id: str) -> bool:
    """Empty one conversation out of the trash immediately, past recovering."""
    session_id = safe_id(session_id)
    if not session_id:
        return False
    home = _trash_home(session_id)
    if not home.is_dir():
        return False
    shutil.rmtree(home, ignore_errors=True)
    return True
