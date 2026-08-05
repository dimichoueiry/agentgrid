"""The daily pad: pages of markdown notes whose checkbox lines are the todos.

The design decision worth keeping: notes and todos are the same document.
Any line matching ``- [ ] text`` in an ordinary page is a live todo, collected
into the rail and tickable from there. There is no separate todo store, because
anything that has to be re-entered somewhere else does not get done, and a
second copy is one more thing able to disagree with the first. Ticking a todo
rewrites exactly one character of the file it lives in, so prose, indentation
and every other line stay byte-identical -- the file is something you also edit
by hand.

Storage is one directory per day and one markdown file per page, with the
filename as the page title:

    ~/.agentgrid/notes/2026-08-05/Notes.md
    ~/.agentgrid/notes/2026-08-05/Kamil sync.md

The filesystem is the source of truth. A page added in Finder appears at the
end of the order; one deleted there falls out of the order rather than leaving
a gap. Arrangement -- page order, groups, pins, per-day collapsed state --
lives outside the notes folder in ``note-meta.json``, so the notes directory
stays pure markdown: greppable, hand-editable, committable.

Order and grouping are global, not per day. How you like your pages arranged
is a standing preference, not a fact about one Tuesday; only the collapsed
state is per day, because folding a group is about the day you are reading.

This module imports nothing from the rest of the package and uses only the
standard library.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

NOTES_DIR = Path.home() / ".agentgrid" / "notes"
META_PATH = Path.home() / ".agentgrid" / "note-meta.json"
DEFAULT_JIRA_BASE = "https://jira.example.com/browse/"
DEFAULT_PAGE = "Notes"
MAX_PAGE_NAME = 48

# The named groups are what make a one-character toggle possible: the box
# character's exact position in the line comes straight out of the match.
TODO_RE = re.compile(
    r"^(?P<indent>\s*)(?P<bullet>[-*])\s+\[(?P<box>[ xX])\]\s?(?P<text>.*)$"
)
JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
MENTION_RE = re.compile(r"@([a-z0-9][a-z0-9._-]*)", re.I)


# --------------------------------------------------------------------------
# Names, paths and validation
# --------------------------------------------------------------------------

def slugify(title: str) -> str:
    """Reduce a title to a mention handle.

    Lowercase, non-alphanumerics collapsed to ``-``, capped at 48 characters.
    A slug rather than the title itself, because a title with spaces has no
    end marker in plain text -- there would be no way to tell where
    ``@pr review ranking`` stopped.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    slug = slug[:MAX_PAGE_NAME].strip("-")
    return slug or "session"


def jira_base() -> str:
    """The ticket-link prefix, overridable with AGENTGRID_JIRA_URL."""
    base = os.environ.get("AGENTGRID_JIRA_URL", "").strip() or DEFAULT_JIRA_BASE
    return base if base.endswith("/") else base + "/"


def _valid_day(value) -> bool:
    """True when *value* is a real YYYY-MM-DD date.

    This doubles as the path guard: the day becomes a directory name, and
    nothing with separators, dots or traversal in it survives strptime.
    """
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def _require_day(day: str) -> str:
    if not _valid_day(day):
        raise ValueError(f"Not a date: {day!r}")
    return str(day)


def _valid_page(name) -> str:
    """Sanitise a page name into something that can be a filename.

    The filename *is* the title, so the title must survive being one:
    whitespace runs collapse to a single space, path-hostile characters
    become ``-``, leading and trailing spaces and dots are stripped (a
    leading dot would hide the file), and the length is capped. An empty
    result falls back to the default page.
    """
    name = re.sub(r"\s+", " ", str(name or ""))
    name = re.sub(r"[/\\:\x00-\x1f]", "-", name)
    name = name.strip(" .")
    name = name[:MAX_PAGE_NAME].strip(" .")
    return name or DEFAULT_PAGE


def path_for(day: str, page: str) -> Path:
    """The file backing one page of one day."""
    return NOTES_DIR / _require_day(day) / f"{_valid_page(page)}.md"


def _migrate_flat(day: str) -> None:
    """Move a pre-pages ``YYYY-MM-DD.md`` to ``YYYY-MM-DD/Notes.md``.

    Lazy, on access, rather than a startup sweep. os.replace so the content
    is moved intact, never rewritten. Skipped when the target already exists
    -- a half-done migration must not clobber notes written since.
    """
    flat = NOTES_DIR / f"{day}.md"
    if not flat.is_file():
        return
    target = NOTES_DIR / day / f"{DEFAULT_PAGE}.md"
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(flat, target)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Due dates written in the sentence
# --------------------------------------------------------------------------

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_ORDINAL = r"(?:st|nd|rd|th)?"
_DAY_SHORT = "mon|tues|tue|thurs|thur|thu|wed|fri|sat|sun"

# Ordered: first match wins, so the specific forms are tried before the loose
# ones. Every pattern is word-anchored -- "the long march of progress" must
# not read as a date.
#
# A bare weekday is deliberately not here: "wednesday sync" is the name of a
# meeting, not a deadline. A weekday counts only when tagged (#mon, #friday)
# or preceded by a cue word.
DUE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(
        rf"\b({_MONTHS})[a-z]*\.?\s+(\d{{1,2}}){_ORDINAL}\b(?:,?\s*(\d{{4}}))?",
        re.I), "md"),
    (re.compile(
        rf"\b(\d{{1,2}}){_ORDINAL}\s+({_MONTHS})[a-z]*\.?\b(?:,?\s*(\d{{4}}))?",
        re.I), "dm"),
    (re.compile(r"\bin\s+(\d{1,3})\s+(day|week|month)s?\b", re.I), "in"),
    (re.compile(r"\b(today|tonight)\b", re.I), "today"),
    (re.compile(r"\b(tomorrow|tmrw)\b", re.I), "tomorrow"),
    (re.compile(r"\bnext\s+week\b", re.I), "nextweek"),
    (re.compile(rf"#({_WEEKDAYS}|{_DAY_SHORT})\b", re.I), "weekday"),
    (re.compile(
        rf"\b(?:on|by|next|due|before|until|till)\s+({_WEEKDAYS}|{_DAY_SHORT})\b",
        re.I), "weekday"),
]

_MONTH_INDEX = {name: number for number, name in enumerate(
    _MONTHS.split("|"), start=1)}
# Three-letter prefixes are unique across the week, which is what lets the
# short forms (thurs, tue) resolve without their own table.
_WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
                  "fri": 4, "sat": 5, "sun": 6}


def _month_day(month_token: str, day_token: str, year_token, anchor: date) -> date:
    month = _MONTH_INDEX[month_token.lower()[:3]]
    day = int(day_token)
    if year_token:
        return date(int(year_token), month, day)
    # A bare "March 3" that falls before the anchor means NEXT March -- a
    # deadline in the past was not what the sentence meant.
    due = date(anchor.year, month, day)
    if due < anchor:
        due = date(anchor.year + 1, month, day)
    return due


def parse_due(text: str, anchor: date):
    """Find a due date written in *text*, resolved against *anchor*.

    The anchor is the day the note belongs to, never today: "tomorrow"
    written on Monday still means Tuesday when read back a fortnight later.
    A note is a record of what you meant when you wrote it.

    Returns ``(iso_date, matched_text)`` or None. A bad date is simply not
    a date -- "february 30" raises inside the construction, is caught, and
    the search continues.
    """
    for pattern, kind in DUE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            if kind == "iso":
                due = date(int(match.group(1)), int(match.group(2)),
                           int(match.group(3)))
            elif kind == "md":
                due = _month_day(match.group(1), match.group(2),
                                 match.group(3), anchor)
            elif kind == "dm":
                due = _month_day(match.group(2), match.group(1),
                                 match.group(3), anchor)
            elif kind == "in":
                # "in N months" is N * 30 days, approximate on purpose --
                # a todo is not a calendar.
                unit = {"day": 1, "week": 7, "month": 30}[match.group(2).lower()]
                due = anchor + timedelta(days=int(match.group(1)) * unit)
            elif kind == "today":
                due = anchor
            elif kind == "tomorrow":
                due = anchor + timedelta(days=1)
            elif kind == "nextweek":
                due = anchor + timedelta(days=7)
            elif kind == "weekday":
                target = _WEEKDAY_INDEX[match.group(1).lower()[:3]]
                # Always forward, never today: "by monday" written on a
                # Monday means the next one.
                due = anchor + timedelta(days=(target - anchor.weekday()) % 7 or 7)
            else:
                continue
        except (ValueError, KeyError):
            continue
        return due.isoformat(), match.group(0)
    return None


# --------------------------------------------------------------------------
# The meta file: arrangement, pins, collapsed state
# --------------------------------------------------------------------------

def load_meta() -> dict:
    """Read note-meta.json, tolerating a missing or malformed file."""
    try:
        loaded = json.loads(META_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def save_meta(meta: dict) -> None:
    """Write note-meta.json atomically, stable enough to diff."""
    try:
        META_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = META_PATH.with_suffix(".json.tmp")
        temp.write_text(json.dumps(meta, indent=2, sort_keys=True),
                        encoding="utf-8")
        os.replace(temp, META_PATH)
    except OSError:
        pass


def _arrangement() -> dict:
    """The global page order and groups.

    Order and grouping were once stored per day, which meant every new
    morning lost them. They are a standing preference, stored once by page
    name -- and seeded here from the most recent day-keyed entry that has
    one, so arrangements made before the change are not lost.
    """
    meta = load_meta()
    order = meta.get("order") if isinstance(meta.get("order"), list) else None
    groups = meta.get("groups") if isinstance(meta.get("groups"), dict) else None
    if order is None or groups is None:
        for key in sorted((k for k in meta if _valid_day(k)), reverse=True):
            entry = meta.get(key)
            if not isinstance(entry, dict):
                continue
            if order is None and isinstance(entry.get("order"), list):
                order = entry["order"]
            if groups is None and isinstance(entry.get("groups"), dict):
                groups = entry["groups"]
            if order is not None and groups is not None:
                break
    return {"order": list(order or []),
            "groups": {str(k): str(v) for k, v in (groups or {}).items() if v}}


# --------------------------------------------------------------------------
# Pages of a day
# --------------------------------------------------------------------------

def existing_pages(day: str) -> list[str]:
    """Whatever .md files are on disk for *day*, alphabetical."""
    _require_day(day)
    _migrate_flat(day)
    folder = NOTES_DIR / day
    try:
        names = [entry.stem for entry in folder.iterdir()
                 if entry.suffix == ".md" and entry.is_file()]
    except OSError:
        return []
    return sorted(names, key=str.lower)


def pages(day: str) -> list[str]:
    """The day's pages in display order.

    Files are the source of truth; the saved order is a preference
    reconciled against them on every read. A page added in Finder appears
    at the end; one deleted there falls out rather than leaving a gap.
    """
    existing = existing_pages(day)
    order = _arrangement()["order"]
    if not order:
        rest = [name for name in existing if name != DEFAULT_PAGE]
        return ([DEFAULT_PAGE] if DEFAULT_PAGE in existing else []) + rest
    known = [name for name in order if name in existing]
    return known + [name for name in existing if name not in known]


def page_groups(day: str) -> dict[str, str]:
    """Global groups filtered to pages that exist on *day*."""
    existing = set(existing_pages(day))
    return {page: group for page, group in _arrangement()["groups"].items()
            if page in existing}


def set_order(day: str, order: list[str]) -> None:
    """Record a new page order for *day* without disturbing other days.

    The naive version (today's pages first, everything else appended)
    pushes every page absent from today to the end of the GLOBAL order, so
    tidying a Tuesday moves a Monday-only 1-1 to the back of every Monday.
    Instead the new order is poured into the slots today's pages already
    occupy in the global order; absent pages keep their positions.
    """
    names = existing_pages(day)
    clean = [page for page in (order or []) if page in names]
    clean += [name for name in names if name not in clean]
    old = _arrangement()["order"]
    result: list[str] = []
    incoming = iter(clean)
    for name in old:
        if name in clean:
            nxt = next(incoming, None)
            if nxt is not None:
                result.append(nxt)
        elif name not in result:
            result.append(name)
    result.extend(name for name in incoming if name not in result)
    meta = load_meta()
    meta["order"] = result
    meta.setdefault("groups", _arrangement()["groups"])
    save_meta(meta)


def set_group(day: str, page: str, group) -> None:
    """Assign *page* to a group, globally. Empty or None removes.

    Group position is not stored separately -- a group sits wherever its
    first member sits, so one ordering array governs both and the two
    cannot drift apart, which two arrays would the first time a page was
    dragged out of a group.
    """
    _require_day(day)
    page = _valid_page(page)
    arrangement = _arrangement()
    groups = arrangement["groups"]
    label = re.sub(r"\s+", " ", str(group or "")).strip()[:MAX_PAGE_NAME]
    if label:
        groups[page] = label
    else:
        groups.pop(page, None)
    meta = load_meta()
    meta["order"] = arrangement["order"]
    meta["groups"] = groups
    save_meta(meta)


def move_group(day: str, group: str, before) -> None:
    """Move a whole group so its first member sits in front of *before*."""
    day_pages = pages(day)
    groups_map = page_groups(day)
    members = [page for page in day_pages if groups_map.get(page) == group]
    if not members:
        return
    rest = [page for page in day_pages if page not in members]
    index = rest.index(before) if before in rest else len(rest)
    set_order(day, rest[:index] + members + rest[index:])


# --------------------------------------------------------------------------
# Pinned (recurring) pages
# --------------------------------------------------------------------------

def _normalise_pin(entry):
    # Legacy: a pinned entry may be a bare string from before groups and
    # weekdays existed.
    if isinstance(entry, str):
        name = entry.strip()
        return {"name": name, "group": "", "days": []} if name else None
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    if not name:
        return None
    days = []
    for value in entry.get("days") or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 6:
            days.append(number)
    return {"name": name, "group": str(entry.get("group") or ""),
            "days": sorted(set(days))}


def pinned() -> list[dict]:
    """The global list of recurring pages: {name, group, days}.

    An empty days list means every day, so pins made before weekdays
    existed keep working untouched.
    """
    result = []
    for entry in load_meta().get("pinned") or []:
        pin = _normalise_pin(entry)
        if pin is not None and not any(p["name"] == pin["name"] for p in result):
            result.append(pin)
    return result


def set_pinned(name: str, on: bool, group: str = "", days=None) -> None:
    """Pin or unpin a page. days are weekday numbers, 0 = Monday."""
    name = _valid_page(name)
    clean_days = []
    for value in days or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 6:
            clean_days.append(number)
    meta = load_meta()
    pins = [pin for pin in (
        _normalise_pin(entry) for entry in meta.get("pinned") or [])
        if pin is not None and pin["name"] != name]
    if on:
        pins.append({"name": name, "group": str(group or ""),
                     "days": sorted(set(clean_days))})
    meta["pinned"] = pins
    save_meta(meta)


def ensure_pinned(day: str) -> None:
    """Materialise missing pinned pages for *day*.

    The weekday comes from the day being opened, never the clock: browsing
    back to last Friday still shows the Friday page, and a Tuesday never
    conjures one. Structure recurs, content does not -- a fresh page is
    seeded with ``# Name`` and nothing else, because copying unfinished work
    forward would put it in a second place to rot when Carried over already
    surfaces it. (The seed content matters: write_day deletes an empty file,
    so a page seeded blank would vanish under you.)
    """
    weekday = datetime.strptime(_require_day(day), "%Y-%m-%d").date().weekday()
    for pin in pinned():
        if pin["days"] and weekday not in pin["days"]:
            continue
        target = path_for(day, pin["name"])
        if not target.exists():
            write_day(day, f"# {pin['name']}\n\n", pin["name"])
        if pin["group"] and pin["name"] not in _arrangement()["groups"]:
            set_group(day, pin["name"], pin["group"])


# --------------------------------------------------------------------------
# Collapsed groups (the one per-day preference)
# --------------------------------------------------------------------------

def collapsed_groups(day: str) -> list[str]:
    """Groups folded shut on *day*, reconciled against groups that exist."""
    meta = load_meta()
    entry = meta.get(_require_day(day))
    stored = entry.get("collapsed") if isinstance(entry, dict) else None
    labels = set(page_groups(day).values())
    return [group for group in (stored or []) if group in labels]


def set_collapsed(day: str, group: str, shut: bool) -> None:
    """Fold a group shut (or open) for one day."""
    _require_day(day)
    meta = load_meta()
    entry = meta.get(day) if isinstance(meta.get(day), dict) else {}
    current = [g for g in entry.get("collapsed") or [] if isinstance(g, str)]
    if shut and group not in current:
        current.append(group)
    if not shut:
        current = [g for g in current if g != group]
    entry["collapsed"] = current
    meta[day] = entry
    save_meta(meta)


# --------------------------------------------------------------------------
# Every page, across every day
# --------------------------------------------------------------------------

def days_with_notes() -> list[str]:
    """Every day that has notes, newest first.

    Directory names and legacy flat-file stems that parse as ISO dates;
    stray files are ignored rather than reported.
    """
    days = set()
    try:
        entries = list(NOTES_DIR.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_dir() and _valid_day(entry.name):
            days.add(entry.name)
        elif entry.suffix == ".md" and _valid_day(entry.stem):
            days.add(entry.stem)
    return sorted(days, reverse=True)


def all_pages() -> list[dict]:
    """Every page name that exists anywhere, with the facts that decide
    what to do with it.

    Pinned names with no file yet are included, or a page you set to recur
    would be unmanageable until the morning it first appears.
    """
    info: dict[str, dict] = {}
    for day in days_with_notes():  # newest first
        for name in existing_pages(day):
            entry = info.get(name)
            if entry is None:
                entry = info[name] = {"name": name, "days": 0,
                                      "first": day, "last": day,
                                      "group": "", "pinned": False,
                                      "weekdays": []}
            entry["days"] += 1
            entry["first"] = day  # iteration is newest first, so the last
            # assignment leaves the oldest day here.
    groups = _arrangement()["groups"]
    for pin in pinned():
        entry = info.get(pin["name"])
        if entry is None:
            entry = info[pin["name"]] = {"name": pin["name"], "days": 0,
                                         "first": None, "last": None,
                                         "group": "", "pinned": False,
                                         "weekdays": []}
        entry["pinned"] = True
        entry["weekdays"] = pin["days"]
        if pin["group"] and not groups.get(pin["name"]):
            entry["group"] = pin["group"]
    for name, entry in info.items():
        if groups.get(name):
            entry["group"] = groups[name]
    order = _arrangement()["order"]
    position = {name: index for index, name in enumerate(order)}
    return sorted(info.values(),
                  key=lambda e: (position.get(e["name"], len(order)),
                                 e["name"].lower()))


# --------------------------------------------------------------------------
# Reading, writing, deleting, renaming
# --------------------------------------------------------------------------

def read_day(day: str, page: str = DEFAULT_PAGE) -> str:
    """The text of one page of one day; empty string on any failure."""
    try:
        _migrate_flat(_require_day(day))
        return path_for(day, page).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def write_day(day: str, text: str, page: str = DEFAULT_PAGE) -> None:
    """Write one page atomically. Empty text deletes the file.

    Deleting on empty is what keeps the directory honest -- a page exists
    once it has content -- and an emptied day directory is removed so
    days_with_notes does not report a day with nothing in it.
    """
    target = path_for(day, page)
    if not str(text or "").strip():
        try:
            target.unlink()
        except OSError:
            pass
        try:
            target.parent.rmdir()
        except OSError:
            pass
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".md.tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        pass


def append_line(day: str, text: str, page=None) -> None:
    """Append one line to a page, creating the page if needed."""
    page = _valid_page(page) if page else DEFAULT_PAGE
    current = read_day(day, page)
    if current and not current.endswith("\n"):
        current += "\n"
    write_day(day, current + str(text).rstrip("\n") + "\n", page)


def delete_page(day: str, page: str) -> bool:
    """Delete one page from ONE day.

    This is one of two clearly labelled deletes -- a page is not one
    object, it is one file per day that happens to share a name, and
    guessing which delete was meant would be wrong in one direction or
    the other. delete_everywhere is the other.
    """
    page = _valid_page(page)
    target = path_for(day, page)
    removed = False
    try:
        target.unlink()
        removed = True
    except OSError:
        pass
    try:
        target.parent.rmdir()
    except OSError:
        pass
    arrangement = _arrangement()
    meta = load_meta()
    meta["order"] = [name for name in arrangement["order"] if name != page]
    groups = arrangement["groups"]
    groups.pop(page, None)
    meta["groups"] = groups
    # Delete must also unpin: without this the file is removed and the very
    # next request runs ensure_pinned and recreates it, so Delete looks like
    # it silently did nothing -- which is exactly how it behaved for every
    # recurring page.
    meta["pinned"] = [pin for pin in (
        _normalise_pin(entry) for entry in meta.get("pinned") or [])
        if pin is not None and pin["name"] != page]
    save_meta(meta)
    return removed


def delete_everywhere(name: str) -> int:
    """Delete a page from EVERY day. Returns how many files were removed."""
    name = _valid_page(name)
    removed = 0
    for day in days_with_notes():
        if name not in existing_pages(day):
            continue
        target = path_for(day, name)
        try:
            target.unlink()
            removed += 1
        except OSError:
            continue
        try:
            target.parent.rmdir()
        except OSError:
            pass
    arrangement = _arrangement()
    meta = load_meta()
    meta["order"] = [page for page in arrangement["order"] if page != name]
    groups = arrangement["groups"]
    groups.pop(name, None)
    meta["groups"] = groups
    meta["pinned"] = [pin for pin in (
        _normalise_pin(entry) for entry in meta.get("pinned") or [])
        if pin is not None and pin["name"] != name]
    save_meta(meta)
    return removed


def rename_page(day: str, old: str, new: str) -> bool:
    """Rename a page on one day, carrying its order slot and group.

    Refuses -- by raising ValueError, which the API translates to a 409 --
    if the target already exists: os.replace would silently clobber a
    page's content otherwise.
    """
    old = _valid_page(old)
    new = _valid_page(new)
    if old == new:
        return True
    source = path_for(day, old)
    target = path_for(day, new)
    if target.exists():
        raise ValueError(f"A page called '{new}' already exists.")
    if not source.exists():
        raise ValueError(f"No page called '{old}' on {day}.")
    os.replace(source, target)
    arrangement = _arrangement()
    meta = load_meta()
    meta["order"] = [new if name == old else name
                     for name in arrangement["order"]]
    groups = arrangement["groups"]
    if old in groups:
        groups[new] = groups.pop(old)
    meta["groups"] = groups
    save_meta(meta)
    return True


def rename_everywhere(old: str, new: str) -> int:
    """Rename a page across every day it exists on. Returns the count.

    Collisions are checked on ALL days before anything is renamed --
    renaming half the days and then failing would leave the page split
    across two names.
    """
    old = _valid_page(old)
    new = _valid_page(new)
    if old == new:
        return 0
    days = [day for day in days_with_notes() if old in existing_pages(day)]
    clashes = [day for day in days if path_for(day, new).exists()]
    if clashes:
        raise ValueError(
            f"A page called '{new}' already exists on {clashes[0]}.")
    renamed = 0
    for day in days:
        try:
            os.replace(path_for(day, old), path_for(day, new))
            renamed += 1
        except OSError:
            continue
    arrangement = _arrangement()
    meta = load_meta()
    meta["order"] = [new if name == old else name
                     for name in arrangement["order"]]
    groups = arrangement["groups"]
    if old in groups:
        groups[new] = groups.pop(old)
    meta["groups"] = groups
    pins = [pin for pin in (
        _normalise_pin(entry) for entry in meta.get("pinned") or [])
        if pin is not None]
    for pin in pins:
        if pin["name"] == old:
            pin["name"] = new
    meta["pinned"] = pins
    save_meta(meta)
    return renamed


# --------------------------------------------------------------------------
# Todos
# --------------------------------------------------------------------------

def todos_in(day: str, page: str, text=None) -> list[dict]:
    """Every checkbox line of one page, with due dates and ticket keys."""
    if text is None:
        text = read_day(day, page)
    anchor = datetime.strptime(_require_day(day), "%Y-%m-%d").date()
    todos = []
    for index, line in enumerate(text.split("\n")):
        match = TODO_RE.match(line)
        if not match:
            continue
        body = match.group("text")
        due = parse_due(body, anchor)
        todos.append({
            "day": day,
            "page": page,
            "line": index,
            "depth": min(len(match.group("indent").expandtabs(2)) // 2, 4),
            "done": match.group("box").lower() == "x",
            "text": body,
            "due": due[0] if due else None,
            "dueText": due[1] if due else "",
            "tickets": JIRA_RE.findall(body),
        })
    return todos


def todos_for_day(day: str) -> list[dict]:
    """Every page's todos for *day*, concatenated in page order.

    Todos span every page of a day: pages separate the notes from three
    different meetings, but "what do I still owe someone" does not stop at
    a meeting boundary.
    """
    todos = []
    for page in pages(day):
        todos.extend(todos_in(day, page))
    return todos


def toggle(day: str, line: int, page: str = DEFAULT_PAGE) -> bool:
    """Flip one checkbox, rewriting exactly one character of the file.

    The file is something you also edit by hand, so prose, indentation and
    every other line must stay byte-identical -- git diff on the notes
    directory shows a one-character change. Raises IndexError for a line
    that is not there and ValueError for one that is not a todo, for the
    API to translate.
    """
    target = path_for(day, page)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        raise ValueError(f"No page '{page}' on {day}.")
    lines = text.split("\n")
    if not 0 <= int(line) < len(lines):
        raise IndexError(f"No line {line} on {day}/{page}.")
    current = lines[int(line)]
    match = TODO_RE.match(current)
    if not match:
        raise ValueError(f"Line {line} is not a todo.")
    position = match.start("box")
    flipped = " " if match.group("box").lower() == "x" else "x"
    lines[int(line)] = current[:position] + flipped + current[position + 1:]
    write_day(day, "\n".join(lines), page)
    return flipped == "x"


def collect(day: str, span: str = "day") -> dict:
    """The rail's payload: current todos plus carried-over unfinished ones.

    The span windows back from the day being viewed: day 0, week 6,
    month 29. Days BEFORE the window contribute only unfinished items,
    under Carried over -- they are why this is a working list rather than
    a diary, but they are not today's work and must not be mixed into it.
    """
    _require_day(day)
    back = {"day": 0, "week": 6, "month": 29}.get(span, 0)
    viewed = datetime.strptime(day, "%Y-%m-%d").date()
    start = (viewed - timedelta(days=back)).isoformat()
    current: list[dict] = []
    carried: list[dict] = []
    for known_day in days_with_notes():  # newest first
        if known_day > day:
            continue
        if known_day >= start:
            current.extend(todos_for_day(known_day))
        elif len(carried) < 50:
            # Carried over is capped: an unbounded list of everything ever
            # left undone stops being a working list.
            for todo in todos_for_day(known_day):
                if not todo["done"] and len(carried) < 50:
                    carried.append(todo)
    current.sort(key=lambda t: (t["done"], t["day"], t["page"], t["line"]))
    open_count = sum(1 for todo in current if not todo["done"]) + len(carried)
    return {"todos": current, "carried": carried,
            "openCount": open_count, "jiraBase": jira_base()}


# --------------------------------------------------------------------------
# Mentions and history
# --------------------------------------------------------------------------

def mentions_of(slug: str, days_back: int = 120) -> list[dict]:
    """Every line in the pads that mentions ``@slug``.

    Scanned on demand rather than indexed -- a second copy would be one
    more thing able to disagree with the notes. A body with no ``@`` at all
    is skipped before any line work.
    """
    wanted = str(slug or "").lower()
    if not wanted:
        return []
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    lines = []
    for day in days_with_notes():  # newest first
        if day < cutoff:
            break
        for page in existing_pages(day):
            body = read_day(day, page)
            if "@" not in body:
                continue
            for index, line in enumerate(body.split("\n")):
                if wanted not in (m.lower() for m in MENTION_RE.findall(line)):
                    continue
                match = TODO_RE.match(line)
                lines.append({
                    "day": day,
                    "page": page,
                    "line": index,
                    "text": match.group("text") if match else line.strip(),
                    "isTodo": bool(match),
                    "done": bool(match) and match.group("box").lower() == "x",
                })
    return lines


def page_history(page: str, limit: int = 90) -> list[dict]:
    """Everything written on one page across every day, newest first.

    A page is a file per day, so this is the only view where a page reads
    as one thing rather than as the day you happen to be on. Days where
    the page is empty are skipped.
    """
    page = _valid_page(page)
    entries = []
    for day in days_with_notes():  # newest first
        if len(entries) >= limit:
            break
        text = read_day(day, page)
        if not text.strip():
            continue
        open_count = sum(1 for todo in todos_in(day, page, text)
                         if not todo["done"])
        entries.append({"day": day, "text": text, "open": open_count})
    return entries
