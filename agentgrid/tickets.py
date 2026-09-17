"""Tickets: a Jira-shaped work queue that the humans and the agents share.

The founding problem is that a fleet of agents has no common object to point
at. Notes are a person's own thinking and a chat is one session's memory;
neither survives being handed from one agent to another. A ticket does: it has
an id you can say out loud, a state anyone can read, and one owner at a time.

Design decisions worth keeping:

- **One file per ticket**, ``~/.agentgrid/tickets/DC-12.json``. The obvious
  alternative -- a single ``tickets.json`` -- loses writes the moment two
  agents act at once, which here is the normal case, not the edge case. With a
  file each, two agents touching two tickets never contend at all, and the
  store stays greppable and hand-editable like the notes directory.
- **A lock is taken only to mint an id.** Allocation is the one genuinely
  shared resource; every other write is a single-file atomic replace. Holding
  a global lock for each update would serialise a fleet for no benefit.
- **Status and type are small closed sets.** "Or whatever" is how trackers
  rot: a free-text status cannot be a column, cannot be counted and cannot be
  filtered. Labels are the escape hatch for everything else.
- **One activity log, not comments plus history.** Who moved it, who took it
  and what they said are the same question asked three ways, and reading them
  interleaved is the point; the board splits the feed when it draws it.
- **An assignee is a name, optionally bound to a live session.** Sessions die
  and get new ids; the name is what survives. Binding the session id as well
  is what lets the board show a ticket beside the agent working it.
- **Ranks are floats.** Dropping a card between two others is a midpoint, so
  a reorder is one file write rather than a renumber of the column.

This module imports nothing from the rest of the package and uses only the
standard library.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:                                    # POSIX only; without it the store
    import fcntl                        # still works, minus the minting lock.
except ImportError:                     # pragma: no cover - not macOS/Linux
    fcntl = None

TICKETS_DIR = Path.home() / ".agentgrid" / "tickets"
META_NAME = "_meta.json"
LOCK_NAME = ".lock"

# The five that earn a column. "review" is distinct from "in_progress"
# because "waiting on a human" is the state this whole tool exists to surface.
STATUSES = ("backlog", "todo", "in_progress", "review", "done")
STATUS_LABELS = {"backlog": "Backlog", "todo": "To do", "in_progress": "In progress",
                 "review": "In review", "done": "Done"}
# What a person is likely to type for each of them on a command line.
STATUS_ALIASES = {
    "progress": "in_progress", "doing": "in_progress", "start": "in_progress",
    "started": "in_progress", "wip": "in_progress",
    "in_review": "review", "reviewing": "review", "pr": "review",
    "closed": "done", "complete": "done", "completed": "done", "finished": "done",
    "open": "todo", "queued": "todo", "next": "todo", "later": "backlog",
}
OPEN_STATUSES = tuple(s for s in STATUSES if s != "done")

TYPES = ("task", "bug", "story", "spike", "chore")
TYPE_LABELS = {"task": "Task", "bug": "Bug", "story": "Story",
               "spike": "Spike", "chore": "Chore"}

PRIORITIES = ("low", "medium", "high", "urgent")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}

MAX_TITLE = 160
MAX_NAME = 60
MAX_LABEL = 32
MAX_LABELS = 8
MAX_BODY = 20000
MAX_COMMENT = 8000
MAX_ACTIVITY = 250

# A project key is 2-5 uppercase letters/digits and the number is decimal.
# Anything else is not an id, which doubles as the path guard: no separator,
# dot or traversal survives this.
ID_RE = re.compile(r"^[A-Z][A-Z0-9]{1,4}-[0-9]{1,6}$")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,4}$")
NO_PROJECT_KEY = "GEN"

# The fields a plain edit may set. Status and assignee are absent because each
# has its own verb: they are transitions that write an activity line -- and, on
# the board, a message into an agent's chat -- never a quiet overwrite.
#
# Editing `project` re-files a ticket without renumbering it: DC-4 moved to
# another repo stays DC-4. Jira mints a new key there, but an id that changed
# under you would break every mention already written into a commit or a chat,
# and the board shows the project name beside the id anyway.
#
# `area` is a work area's id, or "" for none. It is stored as given: this module
# knows nothing about areas.json, so the callers that do (the board, the CLI)
# check the id names a real area before it gets here.
EDITABLE = ("title", "body", "type", "priority", "labels", "project", "area", "due")
MAX_AREA = 64


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return default
    return loaded if isinstance(loaded, type(default)) else default


def _write_json(path: Path, payload) -> None:
    """Write to a temp file in the same directory, then rename.

    os.replace is atomic on POSIX, so a reader sees either the whole previous
    ticket or the whole new one -- never the half-written file a plain
    open(..., "w") leaves behind when a process dies mid-dump.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


@contextmanager
def _minting_lock():
    """Serialise id allocation across every process on this machine."""
    TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    if fcntl is None:                                   # pragma: no cover
        yield
        return
    handle = os.open(TICKETS_DIR / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _meta() -> dict:
    meta = _read_json(TICKETS_DIR / META_NAME, {})
    meta.setdefault("keys", {})        # project path -> project key
    meta.setdefault("counters", {})    # project key -> highest number minted
    meta.setdefault("aliases", {})     # a retired project key -> the key it became
    return meta


# --------------------------------------------------------------------------
# Project keys
# --------------------------------------------------------------------------

def _candidate_key(name: str) -> str:
    """A Jira-ish project key from a folder name.

    A multi-word name becomes its initials (``my-cool-app`` -> ``MCA``,
    ``AgentGrid`` -> ``AG``); a single word becomes its first four letters
    (``drawcal`` -> ``DRAW``). Initials win where they exist because that is
    what a person would have picked, and a key is read far more than typed.
    """
    words: list[str] = []
    for part in [p for p in re.split(r"[^A-Za-z0-9]+", str(name or "")) if p]:
        # split camelCase / PascalCase, keeping digit runs attached
        words += re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part)
    key = "".join(w[0] for w in words[:4]) if len(words) >= 2 else (words[0] if words else "")[:4]
    key = re.sub(r"[^A-Za-z0-9]", "", key).upper()
    if not key or not key[0].isalpha():
        key = "T" + key
    return key[:5] if len(key) >= 2 else key + "X"


def project_key(project: str) -> str:
    """The stable key for a project path, registering one on first sight.

    A key only changes when a person renames it (`rekey`), and the old one is
    kept as an alias, so an id already written into a commit message, a chat
    or someone's memory still finds its ticket.
    """
    project = str(project or "").strip()
    if not project:
        return NO_PROJECT_KEY
    with _minting_lock():
        meta = _meta()
        existing = meta["keys"].get(project)
        if existing and PROJECT_KEY_RE.match(existing):
            return existing
        base = _candidate_key(Path(project).name)
        taken = set(meta["keys"].values()) | {NO_PROJECT_KEY}
        key, n = base, 1
        while key in taken:
            n += 1
            key = f"{base[:4]}{n}"
        meta["keys"][project] = key
        _write_json(TICKETS_DIR / META_NAME, meta)
        return key


def known_keys() -> dict:
    """project path -> project key, for everything registered so far."""
    return dict(_meta()["keys"])


def _resolve_alias(prefix: str, aliases: dict) -> str:
    seen = set()
    while prefix in aliases and prefix not in seen:    # a key renamed twice
        seen.add(prefix)
        prefix = aliases[prefix]
    return prefix


def rekey(project: str, new_key: str) -> dict:
    """Rename a project's key, and every ticket filed under it with it.

    COS-1..COS-n become NEW-1..NEW-n: a prefix that only applied to future
    tickets would leave one project answering to two names on the board. The
    retired key is remembered as an alias, so `ag ticket show COS-2` and old
    mentions keep resolving. Returns {"from", "to", "renamed"}.
    """
    project = str(project or "").strip()
    new_key = re.sub(r"[^A-Za-z0-9]", "", str(new_key or "")).upper()
    if not PROJECT_KEY_RE.match(new_key):
        raise ValueError("A prefix is 2-5 letters or digits, starting with a letter.")
    if new_key == NO_PROJECT_KEY:
        raise ValueError(f"{NO_PROJECT_KEY} is reserved for tickets with no project.")
    with _minting_lock():
        meta = _meta()
        old_key = meta["keys"].get(project)
        if not old_key:
            raise ValueError("That project has no ticket prefix yet.")
        if new_key == old_key:
            return {"from": old_key, "to": new_key, "renamed": 0}
        if new_key in meta["keys"].values():
            raise ValueError(f"{new_key} is already another project's prefix.")
        if any(TICKETS_DIR.glob(f"{new_key}-*.json")):
            raise ValueError(f"Tickets named {new_key}-… already exist.")
        renamed = 0
        for path in sorted(TICKETS_DIR.glob(f"{old_key}-*.json")):
            if not ID_RE.match(path.stem):
                continue
            os.replace(path, TICKETS_DIR / f"{new_key}-{path.stem.split('-')[1]}.json")
            renamed += 1
        meta["keys"][project] = new_key
        meta["counters"][new_key] = max(int(meta["counters"].get(new_key) or 0),
                                        int(meta["counters"].pop(old_key, 0) or 0))
        # Taking a retired key back into use ends its life as an alias.
        meta["aliases"].pop(new_key, None)
        meta["aliases"][old_key] = new_key
        _write_json(TICKETS_DIR / META_NAME, meta)
    return {"from": old_key, "to": new_key, "renamed": renamed}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _clean(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _one_of(value, allowed: tuple, fallback: str, aliases: dict | None = None) -> str:
    value = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if aliases and value in aliases:
        return aliases[value]
    return value if value in allowed else fallback


def status_of(value, fallback: str = "") -> str:
    """A status from whatever someone typed, or "" when it is not one."""
    return _one_of(value, STATUSES, fallback, STATUS_ALIASES)


def _labels(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        label = re.sub(r"\s+", "-", str(item or "").strip().lower())[:MAX_LABEL]
        label = re.sub(r"[^a-z0-9._/-]", "", label).strip("-")
        if label and label not in out:
            out.append(label)
    return out[:MAX_LABELS]


def _due(value) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"A due date is YYYY-MM-DD, not {value!r}.")
    return value


def ticket_path(ticket_id: str) -> Path:
    ticket_id = str(ticket_id or "").strip().upper()
    if not ID_RE.match(ticket_id):
        raise ValueError(f"Not a ticket id: {ticket_id or '(empty)'}")
    path = TICKETS_DIR / f"{ticket_id}.json"
    if not path.exists():
        # An id from before its project was re-keyed still finds the ticket.
        prefix, number = ticket_id.split("-")
        current = _resolve_alias(prefix, _meta()["aliases"])
        if current != prefix and PROJECT_KEY_RE.match(current):
            return TICKETS_DIR / f"{current}-{number}.json"
    return path


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _activity(raw) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        entry = {"at": str(item.get("at") or ""),
                 "who": str(item.get("who") or "someone"),
                 "kind": str(item.get("kind") or "")}
        for name in ("text", "from", "to"):
            if item.get(name):
                entry[name] = str(item[name])
        out.append(entry)
    return out[-MAX_ACTIVITY:]


def _normalise(raw: dict, ticket_id: str) -> dict:
    """Fill in anything a hand-edited or older file is missing.

    The store is meant to be editable by hand, so reading has to tolerate a
    file that is only mostly a ticket.
    """
    project = str(raw.get("project") or "")
    try:
        due = _due(raw.get("due"))
    except ValueError:
        due = ""                      # a bad date by hand is dropped, not fatal
    return {
        "id": ticket_id,
        "key": ticket_id.split("-")[0],
        "number": int(ticket_id.split("-")[1]),
        "title": _clean(raw.get("title"), MAX_TITLE) or "(untitled)",
        "body": _clean(raw.get("body") or raw.get("description"), MAX_BODY),
        "type": _one_of(raw.get("type"), TYPES, "task"),
        "status": _one_of(raw.get("status"), STATUSES, "todo", STATUS_ALIASES),
        "priority": _one_of(raw.get("priority"), PRIORITIES, "medium"),
        "project": project,
        "projectName": str(raw.get("projectName") or "") or (Path(project).name if project else ""),
        "area": _clean(raw.get("area"), MAX_AREA),
        "assignee": _clean(raw.get("assignee"), MAX_NAME),
        "sessionId": str(raw.get("sessionId") or ""),
        "reporter": _clean(raw.get("reporter"), MAX_NAME),
        "labels": _labels(raw.get("labels")),
        "due": due,
        "rank": float(raw.get("rank") or 0.0),
        "created": str(raw.get("created") or ""),
        "updated": str(raw.get("updated") or raw.get("created") or ""),
        "closed": str(raw.get("closed") or ""),
        "activity": _activity(raw.get("activity")),
    }


def get(ticket_id: str) -> dict:
    """One ticket. Raises ValueError on a malformed or unknown id."""
    path = ticket_path(ticket_id)
    raw = _read_json(path, {})
    if not raw:
        raise ValueError(f"No such ticket: {path.stem}")
    return _normalise(raw, path.stem)


def load(ticket_id: str):
    """One ticket, or None when it is not there. Still raises on a bad id."""
    try:
        return get(ticket_id)
    except ValueError as error:
        if str(error).startswith("No such ticket"):
            return None
        raise


def all_tickets() -> list[dict]:
    """Every ticket on disk, in the order a board draws them.

    Sorted by column then rank, so a caller can bucket by status without
    sorting again and a card never jumps position between two reads.
    """
    out: list[dict] = []
    try:
        entries = sorted(TICKETS_DIR.glob("*.json"))
    except OSError:
        return out
    for path in entries:
        if path.name == META_NAME or not ID_RE.match(path.stem):
            continue
        raw = _read_json(path, {})
        if raw:
            out.append(_normalise(raw, path.stem))
    column = {s: i for i, s in enumerate(STATUSES)}
    out.sort(key=lambda t: (column.get(t["status"], 99), t["rank"], t["key"], t["number"]))
    return out


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _log(ticket: dict, who: str, kind: str, text: str = "",
         was: str = "", now: str = "") -> None:
    entry = {"at": _now(), "who": _clean(who, MAX_NAME) or "someone", "kind": kind}
    if text:
        entry["text"] = _clean(text, MAX_COMMENT)
    if was:
        entry["from"] = was
    if now:
        entry["to"] = now
    ticket["activity"] = (ticket.get("activity") or [])[-(MAX_ACTIVITY - 1):] + [entry]


def _persist(ticket: dict) -> dict:
    ticket["updated"] = _now()
    _write_json(ticket_path(ticket["id"]), ticket)
    return ticket


def _next_rank(status: str) -> float:
    """One past the last card in that column, so new work lands at the bottom
    rather than silently at the top of somebody's queue."""
    ranks = [t["rank"] for t in all_tickets() if t["status"] == status]
    return (max(ranks) + 1.0) if ranks else 1.0


def create(title: str, *, body: str = "", type: str = "task", status: str = "todo",
           priority: str = "medium", project: str = "", project_name: str = "",
           assignee: str = "", session_id: str = "", reporter: str = "",
           labels=None, due: str = "", area: str = "") -> dict:
    """Mint a ticket. A title is the only requirement, on purpose: a ticket
    you could not file in one line would not get filed."""
    title = _clean(title, MAX_TITLE)
    if not title:
        raise ValueError("A ticket needs a title.")
    due = _due(due)
    project = str(project or "").strip()
    prefix = project_key(project)
    with _minting_lock():
        meta = _meta()
        number = int(meta["counters"].get(prefix) or 0) + 1
        # Never reuse a number whose file still exists: a store restored from
        # a backup, or a counter lost with _meta.json, must not overwrite work.
        while (TICKETS_DIR / f"{prefix}-{number}.json").exists():
            number += 1
        meta["counters"][prefix] = number
        _write_json(TICKETS_DIR / META_NAME, meta)
        ticket_id = f"{prefix}-{number}"
    ticket = _normalise({
        "title": title, "body": body, "type": type, "status": status,
        "priority": priority, "project": project, "area": area,
        "projectName": project_name or (Path(project).name if project else ""),
        "assignee": assignee, "sessionId": session_id, "reporter": reporter,
        "labels": labels, "due": due, "created": _now(),
    }, ticket_id)
    ticket["rank"] = _next_rank(ticket["status"])
    if ticket["status"] == "done":
        ticket["closed"] = _now()
    _log(ticket, reporter or assignee or "you", "created")
    if ticket["assignee"]:
        _log(ticket, reporter or "you", "assigned", now=ticket["assignee"])
    return _persist(ticket)


def update(ticket_id: str, changes: dict, who: str = "") -> dict:
    """Change a ticket's ordinary fields, with one activity line for the lot.

    One line rather than one per field because an edit is a single human act;
    a feed with five entries for one save buries the moves and the comments
    that actually matter.
    """
    ticket = get(ticket_id)
    touched = []
    for field in EDITABLE:
        if field not in changes:
            continue
        value = changes[field]
        if field == "labels":
            value = _labels(value)
        elif field == "type":
            value = _one_of(value, TYPES, ticket["type"])
        elif field == "priority":
            value = _one_of(value, PRIORITIES, ticket["priority"])
        elif field == "due":
            value = _due(value)
        elif field == "title":
            value = _clean(value, MAX_TITLE) or ticket["title"]
        elif field == "body":
            value = _clean(value, MAX_BODY)
        elif field == "project":
            value = str(value or "").strip()
        elif field == "area":
            value = _clean(value, MAX_AREA)
        if ticket[field] == value:
            continue
        ticket[field] = value
        if field == "project":
            ticket["projectName"] = Path(value).name if value else ""
        touched.append({"body": "the description", "area": "the work area"}.get(field, field))
    if touched:
        _log(ticket, who, "edited", text=", ".join(touched))
    return _persist(ticket)


def move(ticket_id: str, status: str, who: str = "", rank=None) -> dict:
    """Change a ticket's column, and optionally its place in it."""
    ticket = get(ticket_id)
    target = status_of(status)
    if not target:
        raise ValueError(f"Not a status: {status!r} (use {', '.join(STATUSES)})")
    was = ticket["status"]
    ticket["status"] = target
    ticket["rank"] = float(rank) if rank is not None else (
        ticket["rank"] if was == target else _next_rank(target))
    # "closed" must never outlive being done, or it starts lying.
    if target == "done" and was != "done":
        ticket["closed"] = _now()
    elif target != "done":
        ticket["closed"] = ""
    if was != target:
        _log(ticket, who, "moved", was=was, now=target)
    return _persist(ticket)


def reorder(ticket_id: str, status: str, before: str = "", who: str = "") -> dict:
    """Place a ticket in a column, immediately above `before` (or last).

    The new rank is the midpoint between its neighbours, so inserting never
    renumbers the column: the operation stays one file write even on a board
    with a thousand tickets.
    """
    target = status_of(status)
    if not target:
        raise ValueError(f"Not a status: {status!r} (use {', '.join(STATUSES)})")
    ticket_id = str(ticket_id).strip().upper()
    column = [t for t in all_tickets() if t["status"] == target and t["id"] != ticket_id]
    rank = None
    if before:
        index = next((i for i, t in enumerate(column)
                      if t["id"] == str(before).strip().upper()), None)
        if index == 0:
            rank = column[0]["rank"] - 1.0
        elif index is not None:
            rank = (column[index - 1]["rank"] + column[index]["rank"]) / 2.0
    elif column:
        rank = column[-1]["rank"] + 1.0
    return move(ticket_id, target, who=who, rank=rank)


def assign(ticket_id: str, assignee: str, who: str = "", session_id: str = "",
           start: bool = False) -> dict:
    """Hand a ticket to someone. `start` also moves it into In progress, so
    claiming and starting are one write rather than two."""
    ticket = get(ticket_id)
    assignee = _clean(assignee, MAX_NAME)
    if ticket["assignee"] != assignee:
        _log(ticket, who or assignee or "you", "assigned",
             was=ticket["assignee"], now=assignee or "nobody")
    ticket["assignee"] = assignee
    ticket["sessionId"] = str(session_id or "")
    _persist(ticket)
    if start and assignee and ticket["status"] in ("backlog", "todo"):
        return move(ticket_id, "in_progress", who=who or assignee)
    return ticket


def take(ticket_id: str, who: str, session_id: str = "") -> dict:
    """Claim a ticket and start it, in one step.

    One call rather than assign-then-move: the two-step version leaves a
    window where a ticket is claimed but still reads as unstarted, and an
    agent that died between them looks like it never picked anything up.
    """
    who = _clean(who, MAX_NAME)
    if not who:
        raise ValueError("Say who is taking it.")
    ticket = get(ticket_id)
    if ticket["assignee"] and ticket["assignee"] != who and ticket["status"] == "in_progress":
        raise ValueError(f"{ticket['id']} is already in progress with "
                         f"{ticket['assignee']}. Use `ag ticket assign` to take it anyway.")
    assign(ticket_id, who, who=who, session_id=session_id)
    return move(ticket_id, "in_progress", who=who)


def comment(ticket_id: str, who: str, text: str) -> dict:
    ticket = get(ticket_id)
    text = _clean(text, MAX_COMMENT)
    if not text:
        raise ValueError("Nothing to say.")
    _log(ticket, who, "comment", text=text)
    return _persist(ticket)


def delete(ticket_id: str) -> bool:
    try:
        ticket_path(ticket_id).unlink()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------

def query(*, project: str = "", status: str = "", type: str = "",
          assignee: str = "", label: str = "", text: str = "", area: str = "",
          open_only: bool = False, limit: int = 0) -> list[dict]:
    """Filter the board. Every argument is optional and ANDed with the rest.

    `status` and `type` take a comma-separated list, because "everything but
    done" is the query people actually want and it is four statuses, not one.
    """
    want_status = {s for s in (status_of(v) for v in str(status).split(",")) if s}
    want_type = {t for t in (_one_of(v, TYPES, "") for v in str(type).split(",")) if t}
    needle = str(text or "").strip().lower()
    label = str(label or "").strip().lower()
    assignee = str(assignee or "").strip().lower()
    project = str(project or "").strip()
    area = str(area or "").strip()
    out = []
    for ticket in all_tickets():
        if area and ticket["area"] != area:
            continue
        if project and project not in (ticket["project"], ticket["projectName"]):
            continue
        if open_only and ticket["status"] == "done":
            continue
        if want_status and ticket["status"] not in want_status:
            continue
        if want_type and ticket["type"] not in want_type:
            continue
        if assignee:
            if assignee in ("none", "unassigned", "nobody", "-"):
                if ticket["assignee"]:
                    continue
            elif ticket["assignee"].lower() != assignee:
                continue
        if label and label not in ticket["labels"]:
            continue
        if needle and needle not in (
                f"{ticket['id']} {ticket['title']} {ticket['body']} {ticket['assignee']} "
                f"{ticket['projectName']} {' '.join(ticket['labels'])}").lower():
            continue
        out.append(ticket)
    return out[:limit] if limit else out


def board(tickets=None) -> dict:
    """The whole board plus the facets a filter bar needs.

    Facets come from what exists rather than from the closed sets, so a
    dropdown never offers a project or a name with nothing behind it.
    """
    tickets = all_tickets() if tickets is None else tickets
    projects: dict[str, dict] = {}
    assignees: dict[str, int] = {}
    labels: dict[str, int] = {}
    counts = {s: 0 for s in STATUSES}
    types = {t: 0 for t in TYPES}
    for ticket in tickets:
        counts[ticket["status"]] = counts.get(ticket["status"], 0) + 1
        types[ticket["type"]] = types.get(ticket["type"], 0) + 1
        entry = projects.setdefault(ticket["project"], {
            "path": ticket["project"], "name": ticket["projectName"] or "No project",
            "key": ticket["key"], "count": 0, "open": 0})
        entry["count"] += 1
        if ticket["status"] != "done":
            entry["open"] += 1
        if ticket["assignee"]:
            assignees[ticket["assignee"]] = assignees.get(ticket["assignee"], 0) + 1
        for label in ticket["labels"]:
            labels[label] = labels.get(label, 0) + 1
    filed: dict[str, int] = {}
    for ticket in tickets:
        filed[ticket["key"]] = filed.get(ticket["key"], 0) + 1
    prefixes = sorted(({"path": path, "name": Path(path).name, "key": key,
                        "count": filed.get(key, 0)} for path, key in known_keys().items()),
                      key=lambda p: p["name"].lower())
    return {
        "tickets": tickets,
        "prefixes": prefixes,
        "statuses": [{"key": s, "label": STATUS_LABELS[s], "count": counts[s]} for s in STATUSES],
        "types": [{"key": t, "label": TYPE_LABELS[t], "count": types[t]} for t in TYPES],
        "priorities": list(PRIORITIES),
        "projects": sorted(projects.values(), key=lambda p: (not p["path"], p["name"].lower())),
        "assignees": sorted(({"name": k, "count": v} for k, v in assignees.items()),
                            key=lambda a: a["name"].lower()),
        "labels": sorted(({"name": k, "count": v} for k, v in labels.items()),
                         key=lambda l: (-l["count"], l["name"])),
        "unassigned": sum(1 for t in tickets if not t["assignee"] and t["status"] != "done"),
    }


def whoami(default: str = "") -> str:
    """The name an agent files and claims work under.

    AGENTGRID_AGENT is set by the spawner, so an agent the board started knows
    the name the board shows it under. The rest is fallback for an agent a
    human started by hand.
    """
    for name in ("AGENTGRID_AGENT", "AGENTGRID_HANDLE"):
        value = os.environ.get(name, "").strip()
        if value:
            return _clean(value, MAX_NAME)
    if default.strip():
        return _clean(default, MAX_NAME)
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_HOME"):
        return "codex"
    return os.environ.get("USER", "you")


# --------------------------------------------------------------------------
# Handing a ticket to an agent
# --------------------------------------------------------------------------

HANDOFF_COMMANDS = """Report back on the ticket as you go:
  ag ticket comment {id} "what you found"
  ag ticket move {id} review     # ready for a human to look at
  ag ticket done {id}            # finished and verified
Read it again any time with `ag ticket show {id}`."""


def handoff_prompt(ticket: dict, assignee: str = "") -> str:
    """The briefing an agent receives when a ticket is handed to it.

    The commands are spelled out rather than assumed: an agent told only "you
    own DC-4" has no way to know this tracker exists, and a ticket nobody can
    close is worse than no ticket at all.
    """
    lines = [f"You have been assigned {ticket['id']}: {ticket['title']}",
             f"{TYPE_LABELS.get(ticket['type'], ticket['type'])} · "
             f"{ticket['priority']} priority · now {STATUS_LABELS[ticket['status']]}"]
    if assignee:
        lines.append(f'You are "{assignee}" on the agentgrid board.')
    if ticket.get("projectName"):
        lines.append(f"Project: {ticket['projectName']} ({ticket['project']})")
    if ticket.get("due"):
        lines.append(f"Due: {ticket['due']}")
    if ticket.get("labels"):
        lines.append("Labels: " + ", ".join(ticket["labels"]))
    if ticket.get("body"):
        lines += ["", ticket["body"]]
    comments = [a for a in ticket.get("activity") or [] if a.get("kind") == "comment"]
    if comments:
        lines += ["", "Notes so far:"]
        lines += [f"- {c.get('who')}: {c.get('text')}" for c in comments[-5:]]
    lines += ["", HANDOFF_COMMANDS.format(id=ticket["id"])]
    return "\n".join(lines)
