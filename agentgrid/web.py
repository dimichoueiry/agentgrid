"""The browser front end's server: a background poller and a small HTTP API.

The capability ceiling is stated up front because the limit is structural
rather than a missing feature: an interactive session's keyboard belongs to
the pty inside the emulator that owns it, and no other process can write to
it. Everything this server does to a session is therefore either read-only
(transcripts, status) or goes through an interface Claude Code already
exposes (`claude --bg`, `claude attach`, `claude stop`). The board can watch
anything and can only drive what the CLI lets it drive.

Design decisions worth keeping:

- One background poller (`Fleet`), not per-request polling. A full refresh
  costs roughly half a second, mostly the `claude agents` subprocess. Polling
  on a daemon thread means a browser refresh never waits on the CLI, and ten
  open tabs cost the same as one.
- Standard library only. `http.server.ThreadingHTTPServer` rather than
  Flask/FastAPI, because on the target network `pip install` does not work
  at all -- see C1 in the PRD.
- Loopback only and token-gated. This endpoint starts processes, so a bare
  localhost port would let any page in the browser drive it. The token is
  minted per run and carried in the printed URL.
"""

from __future__ import annotations

import base64
import binascii
import collections
import html
import io
import json
import os
import queue
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentgrid import areas, chat, discovery, models, notes, sync, teams, terminal, tickets, transcript, voice, workflows, credentials, openrouter

STATIC = Path(__file__).resolve().parent / "static"
POLL_SECONDS = 2.0
DEFAULT_PROJECT_ROOTS = [Path.home(), Path.home() / "Documents" / "GitHub"]
PROJECTS_TTL = 30.0

# Chat attachments land beside the app's other state under ~/.agentgrid, one
# directory per session. The cap is on the decoded file; the body cap sits
# above it with room for base64's ~4/3 inflation plus the JSON envelope, so an
# in-bounds file is never rejected by the outer guard.
UPLOADS_DIR = Path.home() / ".agentgrid" / "uploads"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024

# Batches released from the web-review overlay for a "New agent" target (no
# running session chosen) are parked here so nothing is lost before pickup.
REVIEWS_DIR = Path.home() / ".agentgrid" / "reviews"
# The overlay is injected onto the user's own dev site (another origin), so a
# small, explicit set of routes answers cross-origin. Everything else stays
# same-origin only. The per-run token is still required on every one of them.
CORS_PATHS = ("/overlay.js", "/api/sessions", "/api/review/release")

# Magic-byte signatures for the image types the chat can paste. Content-type is
# never trusted -- the client controls it -- so the bytes themselves decide, and
# a file that is not really an image is refused before it touches disk.
IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def image_extension(data: bytes) -> str | None:
    """The file extension for `data` if it is a supported image, else None.

    WebP is checked separately: its signature is a RIFF container whose type
    tag sits at byte 8, not at the very start.
    """
    for signature, ext in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# Beyond images, the chat takes the files an agent can actually open: PDFs
# (Claude Code's Read renders them), UTF-8 text of any kind (code, logs, CSV,
# JSON, Markdown...), and the zipped Office formats, which an agent unpacks
# with its own tools. Everything else is refused with a message naming these.
OFFICE_EXTENSIONS = frozenset({"docx", "xlsx", "pptx"})
SUPPORTED_ATTACHMENTS = ("an image (PNG, JPEG, GIF, WebP), a PDF, a text or code "
                         "file, or a Word, Excel or PowerPoint document")


def client_extension(name: object) -> str:
    """The lower-cased extension of a client-supplied filename, or "".

    Only a short alphanumeric suffix counts, so the extension can be reused on
    disk without ever carrying a separator or anything else unexpected.
    """
    match = re.search(r"\.([A-Za-z0-9]{1,10})$", Path(str(name or "")).name)
    return match.group(1).lower() if match else ""


def attachment_type(data: bytes, name: object = "") -> tuple[str, str] | None:
    """(kind, extension) for a file the chat can attach, else None.

    The bytes decide the kind -- the client's name only picks which extension a
    text file keeps (so `app.tsx` stays `.tsx`) and which Office format a zip
    claims to be, and even then the zip must really be an Office package.
    kind is one of "image", "pdf", "document" or "text".
    """
    ext = image_extension(data)
    if ext:
        return "image", ext
    if data.startswith(b"%PDF-"):
        return "pdf", "pdf"
    claimed = client_extension(name)
    if data.startswith(b"PK\x03\x04"):
        if claimed not in OFFICE_EXTENSIONS:
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                if "[Content_Types].xml" not in package.namelist():
                    return None
        except (zipfile.BadZipFile, OSError, ValueError):
            return None
        return "document", claimed
    # Text: no NUL bytes and valid UTF-8 (a BOM is fine). Binary formats almost
    # always carry a NUL early on, and fail the decode when they don't.
    if b"\x00" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # a text file named like a binary format would mislead whoever opens it
    if claimed in OFFICE_EXTENSIONS or claimed in ("pdf", "png", "jpg", "jpeg", "gif", "webp"):
        claimed = "txt"
    return "text", claimed or "txt"


def safe_stem(name: str) -> str:
    """A filesystem-safe label from a client-supplied name, no directory parts.

    Any path the client sends is reduced to its final component and then to a
    conservative character set, so a crafted name can neither traverse out of
    the uploads directory nor smuggle a separator into the filename.
    """
    stem = Path(str(name or "")).name
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", stem)          # drop the extension
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    return stem[:48] or "paste"


def decode_image_data(data: str) -> tuple[bytes, str]:
    """Decode a base64 image (bare or a `data:` URL) to (bytes, extension).

    Raises ValueError on anything that is not a real, in-bounds PNG/JPEG/GIF/
    WebP -- the type is decided by magic bytes, never the client's label. Shared
    by the chat paste path and the review overlay's marked-up screenshots.
    """
    if data.startswith("data:"):
        comma = data.find(",")
        data = data[comma + 1:] if comma >= 0 else ""
    raw = base64.b64decode(data, validate=True)
    if not raw:
        raise ValueError("empty image")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("image too large")
    ext = image_extension(raw)
    if ext is None:
        raise ValueError("not a supported image")
    return raw, ext


def session_uploads_dir(session_id: str) -> Path:
    """The uploads directory for one session.

    Sanitised identically on the write side and the validation side, so the two
    always agree on where a session's images live -- a session id is a UUID in
    practice, but never trusted as a raw path component regardless.
    """
    return UPLOADS_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "-", session_id) or "session")

# The CLI colours its output, so anything read back from it must be stripped
# of escape sequences before a regex can find the job id in it.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
JOB_ID_RE = re.compile(r"\b([0-9a-f]{8})\b")


def _slug(title: str | None) -> str:
    """Slug handle for a session, matching what the @mention menu inserts.

    A title with spaces has no end marker in plain text, so the handle written
    after an `@` has to be a slug. Same rules as notes.slugify so the two ends
    of a mention agree on the spelling.
    """
    text = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return text[:48] or "session"


def _valid_day(value: str | None) -> bool:
    """True when `value` is a real ISO date.

    The day string becomes a directory name under ~/.agentgrid/notes, so this
    doubles as the path guard -- no separators, dots or traversal survive
    strptime. notes.py guards again on its side; the API refuses early so a
    bad day is a 400 rather than a silent no-op.
    """
    try:
        datetime.strptime(str(value or ""), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


class EventHub:
    """Fan-out of session status changes, for `GET /api/events`.

    The board polls; a companion (Chief of Staff) should not have to. Every
    poll is shown to observe(), which turns status changes into events and
    hands them to every open stream. The last KEEP events are kept so a
    subscriber that drops and comes back with `Last-Event-ID` gets what it
    missed and nothing twice. Ids carry a per-run prefix: after a restart the
    counter starts over, and a stale id from the old run replays nothing.
    """

    KEEP = 200
    SETTLED = ("done", "failed", "blocked", "idle", "stopped", "complete")

    def __init__(self) -> None:
        self.run = secrets.token_hex(4)
        self._lock = threading.Lock()
        self._seen: dict[str, str] = {}
        self._primed = False
        self._count = 0
        self._log: collections.deque = collections.deque(maxlen=self.KEEP)
        self._subscribers: list[queue.Queue] = []

    def observe(self, sessions: list[dict]) -> list[dict]:
        """One poll's sessions (already overlaid). Returns the events it produced."""
        events: list[dict] = []
        with self._lock:
            now = {str(s.get("sessionId") or ""): s for s in sessions if s.get("sessionId")}
            if self._primed:
                for session_id, session in now.items():
                    was = self._seen.get(session_id)
                    status = str(session.get("status") or "unknown")
                    if was is not None and was != status:
                        events.append(self._event(session, was, status))
            self._seen = {sid: str(s.get("status") or "unknown") for sid, s in now.items()}
            self._primed = True
            for event in events:
                self._count += 1
                event["id"] = f"{self.run}-{self._count}"
                self._log.append(event)
            channels = list(self._subscribers)
        for event in events:
            for channel in channels:
                channel.put(event)
        return events

    @staticmethod
    def _event(session: dict, was: str, status: str) -> dict:
        return {
            "type": "status",
            "sessionId": session.get("sessionId"),
            "title": session.get("title") or session.get("customName") or "",
            "engine": session.get("engine") or "claude",
            "cwd": session.get("cwd") or "",
            "project": session.get("project") or "",
            "from": was,
            "to": status,
            # The turn ended: the agent is done, failed, waiting on you, or was stopped.
            "finished": was == "working" and status in EventHub.SETTLED,
            "at": datetime.now().isoformat(timespec="seconds"),
        }

    def subscribe(self, last_event_id: str = "") -> queue.Queue:
        """A channel of events to come, first replaying those after `last_event_id` (this run only)."""
        channel: queue.Queue = queue.Queue()
        after = 0
        run, _, number = (last_event_id or "").rpartition("-")
        if run == self.run and number.isdigit():
            after = int(number)
        with self._lock:
            if after:
                for event in self._log:
                    if int(event["id"].rpartition("-")[2]) > after:
                        channel.put(event)
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)


def codex_chat_block(session, chat_manager) -> str | None:
    """Why a chat turn must not be started on this codex session, or None.

    Codex allows one writer per thread. A turn this panel started is fine to
    queue behind, but a thread held by something else has to be refused up
    front, with the reason: an interactive `codex` (the card carries its pid)
    keeps the thread for as long as its terminal is open, and a headless run
    still working outside this chat keeps it until it finishes. Sending
    anyway would fail inside the CLI with "already has an active writer".
    """
    if getattr(session, "engine", "claude") != "codex":
        return None
    if chat_manager.state(session.session_id).get("running"):
        return None
    if getattr(session, "pid", None):
        return ("This Codex session is open in a terminal, which holds its thread. "
                "Type there (Open in Terminal), or close that terminal to chat from here.")
    if session.status == "working":
        return ("Codex is still running outside this chat. Wait for it to finish, "
                "then send your message.")
    return None


def _overlay_chat(sessions: list[dict], chat_manager) -> list[dict]:
    """`claude agents` never reports a headless chat turn (`claude -p --resume`) as the
    session working, yet the panel is driving exactly that. The ChatManager knows, so a
    session with a chat turn in flight is shown Working."""
    for session in sessions:
        if chat_manager.state(session["sessionId"]).get("running"):
            session["status"] = "working"
    return sessions


class Fleet:
    """Polls the fleet on a daemon thread and holds the latest snapshot.

    The lock guards `_sessions`, `_error`, `_polled_at` and `_pending_names`.
    On a poll error the last good snapshot is kept and only the error message
    is replaced -- a transient CLI hiccup must not blank the board.
    """

    def __init__(self) -> None:
        self._cache = discovery.TranscriptCache()
        self._lock = threading.Lock()
        self._sessions: list = []
        self._error: str | None = None
        self._polled_at: float = 0.0
        self._pending_names: dict[str, str] = {}
        # Codex prints no job id to wait on, so a name is held against the
        # (cwd, spawn time) instead and applied to the first codex session
        # that appears there afterwards.
        self._pending_codex: list[dict] = []
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Status changes go out as events (see EventHub); `overlay`, set by serve(),
        # applies the chat's own live signal first so the events match /api/sessions.
        self.events = EventHub()
        self.overlay = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()

    def _run(self) -> None:
        while not self._halt.is_set():
            self._poll_once()
            self._halt.wait(POLL_SECONDS)

    def _poll_once(self) -> None:
        sessions, error = discovery.collect(self._cache)
        with self._lock:
            if error:
                # Keep the last good snapshot; only the error message changes.
                self._error = error
            else:
                areas.resolve(sessions)
                self._apply_pending_names(sessions)
                self._sessions = sessions
                self._error = None
            self._polled_at = time.time()
        if not error:
            self._announce(sessions)

    def _announce(self, sessions: list) -> None:
        """Show this poll to the event hub. Never lets a fault there stop the polling."""
        try:
            projected = [self._as_json(session) for session in sessions]
            if self.overlay is not None:
                projected = self.overlay(projected)
            self.events.observe(projected)
        except Exception:  # noqa: BLE001 -- the board must keep polling whatever happens here
            pass

    def name_when_seen(self, job_id: str, name: str) -> None:
        """Hold a name against a job id until the session exists.

        Names are keyed by session id, and the session id does not exist until
        the daemon has created it. So the name waits on the job id, which
        `claude --bg` prints, and is written on first sighting.
        """
        with self._lock:
            self._pending_names[job_id] = name

    def name_codex_when_seen(self, cwd: str, name: str) -> None:
        """Hold a name for the next codex session to appear in a directory."""
        self._name_by_cwd_when_seen(cwd, name, "codex", None)

    def name_interactive_when_seen(self, cwd: str, name: str) -> None:
        """Hold a name for the next interactive claude session in a directory.

        An interactive spawn prints no job id either -- the session id is minted
        inside the terminal, never handed back -- so, exactly like codex, the
        name waits on (cwd, spawn time) and lands on the first matching session.
        """
        self._name_by_cwd_when_seen(cwd, name, "claude", "interactive")

    def _name_by_cwd_when_seen(
        self, cwd: str, name: str, engine: str, kind: str | None
    ) -> None:
        with self._lock:
            self._pending_codex.append(
                {"cwd": cwd, "name": name, "at": time.time(),
                 "engine": engine, "kind": kind}
            )

    def _apply_pending_names(self, sessions: list) -> None:
        # Called with the lock held, after a successful collect.
        for session in sessions:
            wanted = self._pending_names.get(session.job_id or "")
            if wanted:
                discovery.save_custom_name(session.session_id, wanted)
                session.custom_name = wanted
                self._pending_names.pop(session.job_id, None)
        if not self._pending_codex:
            return
        for pending in list(self._pending_codex):
            if time.time() - pending["at"] > 300:
                # The session never appeared; a five-minute-old pending name
                # matching some future session would mislabel it.
                self._pending_codex.remove(pending)
                continue
            want_engine = pending.get("engine", "codex")
            want_kind = pending.get("kind")
            for session in sessions:
                if (
                    getattr(session, "engine", "claude") == want_engine
                    and (want_kind is None or session.kind == want_kind)
                    and session.cwd == pending["cwd"]
                    and not session.custom_name
                    and session.started_at / 1000 >= pending["at"] - 5
                ):
                    discovery.save_custom_name(session.session_id, pending["name"])
                    session.custom_name = pending["name"]
                    self._pending_codex.remove(pending)
                    break

    def snapshot(self) -> dict:
        with self._lock:
            sessions = list(self._sessions)
            error = self._error
            polled_at = self._polled_at
        # Re-sort at projection time: idle_seconds moves on between polls and
        # an override applied since the last poll may have re-banded a card.
        sessions.sort(key=lambda s: s.sort_key)
        return {
            "polledAt": polled_at,
            "error": error,
            "sessions": [self._as_json(session) for session in sessions],
        }

    def raw(self) -> list:
        """The Session objects themselves, for callers that read `cwd`."""
        with self._lock:
            return list(self._sessions)

    @staticmethod
    def _as_json(session) -> dict:
        return {
            "sessionId": session.session_id,
            "jobId": session.job_id,
            "kind": session.kind,
            "status": session.status,
            "realStatus": session.real_status,
            "overridden": session.overridden,
            "unread": session.unread,
            # What to write after an @ to reference this session.
            "slug": _slug(session.display_title),
            "tags": session.tags,
            "project": session.project,
            "cwd": session.cwd,
            "title": session.display_title,
            "customName": session.custom_name,
            "branch": session.git_branch,
            "prompt": session.last_prompt,
            "model": session.model,
            "idleSeconds": round(session.idle_seconds()),
            "toolCounts": session.tool_counts,
            "engine": getattr(session, "engine", "claude"),
            "attachable": session.kind == "background",
            # Whether Open can do anything at all: join a background session,
            # or bring an interactive session's tab to the front. Codex
            # sessions report no pid, so they are honestly un-openable.
            "openable": session.kind == "background" or bool(session.pid),
            "subagents": [
                {
                    "id": agent.agent_id,
                    "type": agent.agent_type,
                    "description": agent.description,
                    "status": agent.status,
                    "messages": agent.messages,
                }
                for agent in session.subagents
            ],
        }


# --- project discovery -------------------------------------------------------

_configured_roots: list[Path] = []
_project_cache: dict = {"at": 0.0, "scanned": set()}


def configure_roots(roots: list[str] | None) -> list[Path]:
    """Decide where to scan for repos. Precedence: --root, then
    AGENTGRID_ROOTS, then the defaults.

    An explicit choice replaces the defaults rather than adding to them --
    having named where your code lives, being shown everything in your home
    directory too is not useful. Nothing is lost by that: directories the live
    fleet is working in are always included whatever the roots. Roots that do
    not exist are dropped rather than raising.
    """
    global _configured_roots
    chosen = [Path(r).expanduser() for r in (roots or [])]
    if not chosen:
        env = os.environ.get("AGENTGRID_ROOTS", "")
        chosen = [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    if not chosen:
        chosen = list(DEFAULT_PROJECT_ROOTS)
    _configured_roots = [root for root in chosen if root.is_dir()]
    _project_cache["at"] = 0.0  # a new set of roots invalidates the cache
    return _configured_roots


def discover_projects(sessions: list) -> list[dict]:
    """Every directory containing `.git`, one level under each root, unioned
    with every directory the live fleet is already working in.

    The scan is one level deep on purpose -- recursing a home directory is
    slow and drags in every cache and node_modules on the disk. The glob is
    cached for PROJECTS_TTL seconds; the live cwds are unioned fresh on every
    call so a session started in an unscanned directory appears immediately.
    """
    now = time.time()
    if now - _project_cache["at"] >= PROJECTS_TTL:
        scanned: set[Path] = set()
        for root in _configured_roots:
            try:
                for git_dir in root.glob("*/.git"):
                    scanned.add(git_dir.parent)
            except OSError:
                continue
        _project_cache["scanned"] = scanned
        _project_cache["at"] = now
    paths: set[Path] = set(_project_cache["scanned"])
    for session in sessions:
        if session.cwd:
            candidate = Path(session.cwd)
            if candidate.is_dir():
                paths.add(candidate)
    # Preferences are applied fresh on every call, on top of the cached scan:
    # a favourite or a hand-added path must not wait out the scan TTL.
    prefs = load_project_prefs()
    for added in prefs["added"]:
        candidate = Path(added)
        if candidate.is_dir():
            paths.add(candidate)
    favourites = set(prefs["favorites"])
    hidden = set(prefs["hidden"])
    ordered = sorted(paths, key=lambda p: (str(p) not in favourites,
                                           p.name.lower(), str(p).lower()))
    by_name: dict[str, int] = {}
    for path in ordered:
        by_name[path.name] = by_name.get(path.name, 0) + 1
    projects = []
    for path in ordered:
        # Two checkouts sharing a folder name are disambiguated by the parent:
        # identical entries in a picker that starts real work is ambiguity
        # worth spending a few characters on.
        label = path.name if by_name[path.name] == 1 else f"{path.name}  ·  {path.parent}"
        projects.append({"path": str(path), "name": path.name, "label": label,
                         "fav": str(path) in favourites,
                         "hidden": str(path) in hidden})
    return projects


# Picker preferences: paths added by hand (the scan is one level deep on
# purpose, so ~/Desktop/anything needs a way in), favourites that float to
# the top, and hidden entries that stop cluttering a list you pick from
# every day. Hiding is a display preference, never a security boundary --
# hidden projects still count as known directories for spawning.

PROJECT_PREFS_PATH = Path.home() / ".agentgrid" / "projects.json"


def load_project_prefs() -> dict:
    try:
        raw = json.loads(PROJECT_PREFS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {key: [str(p) for p in raw.get(key, []) if isinstance(p, str)]
            for key in ("added", "favorites", "hidden")}


def save_project_prefs(prefs: dict) -> None:
    PROJECT_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PROJECT_PREFS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(prefs, indent=2, sort_keys=True), "utf-8")
    os.replace(temporary, PROJECT_PREFS_PATH)


def freeform_root() -> Path:
    """Working directory for a project-less agent -- the Desktop, as if you had
    opened a terminal there and run `claude`. Falls back to home when there is
    no Desktop (a headless box, or a localized one), so a spawn never lands in a
    directory that does not exist.
    """
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def add_project(raw_path: str, create: bool = False) -> tuple[bool, str, bool]:
    """Add a directory to the picker by hand, optionally creating it.

    Returns (ok, message, can_create). `can_create` is True on the one failure
    worth offering a fix for -- a well-formed path that simply does not exist
    yet -- so the caller can offer to make the folder and start a fresh project
    in it. The path is ~-expanded and resolved so it compares sanely.
    """
    raw = str(raw_path or "").strip()
    if not raw:
        return False, "Type a folder path.", False
    candidate = Path(raw).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        return False, "That path could not be resolved.", False
    if not candidate.exists():
        if not create:
            return False, f"{candidate} does not exist yet.", True
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return False, f"Could not create {candidate}: {error}", False
    elif not candidate.is_dir():
        return False, f"{candidate} is a file, not a directory.", False
    prefs = load_project_prefs()
    if str(candidate) not in prefs["added"]:
        prefs["added"].append(str(candidate))
        save_project_prefs(prefs)
    return True, str(candidate), False


def set_project_pref(path: str, kind: str, on: bool) -> None:
    """Flip one path in the favourites or hidden list."""
    prefs = load_project_prefs()
    entries = prefs[kind]
    if on and path not in entries:
        entries.append(path)
    elif not on and path in entries:
        entries.remove(path)
    save_project_prefs(prefs)


# --- starting and joining sessions ------------------------------------------


# --- the agent library -------------------------------------------------------
#
# Reusable agent definitions: a name, an engine, a model and a system prompt,
# saved once and spawnable in one click. This is deliberately just data --
# a definition is nothing but a saved way of launching a real session, so
# everything on the board (attach, stop, transcripts) works on the result.

AGENTS_PATH = Path.home() / ".agentgrid" / "agents.json"
MAX_SYSTEM_PROMPT = 8000


def load_saved_agents() -> list[dict]:
    """The library, oldest first. Missing or corrupt file is an empty library."""
    try:
        raw = json.loads(AGENTS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    agents = []
    for entry in raw:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip():
            agents.append({
                "name": str(entry["name"])[:60],
                "engine": "codex" if entry.get("engine") == "codex" else "claude",
                "model": str(entry.get("model") or ""),
                "systemPrompt": str(entry.get("systemPrompt") or "")[:MAX_SYSTEM_PROMPT],
            })
    return agents


def _write_saved_agents(agents: list[dict]) -> None:
    AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = AGENTS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(agents, indent=2), "utf-8")
    os.replace(temporary, AGENTS_PATH)


def save_saved_agent(name: str, engine: str, model: str, system_prompt: str) -> list[dict]:
    """Save or overwrite one definition by name (case-insensitive)."""
    name = name.strip()[:60]
    agents = [a for a in load_saved_agents() if a["name"].lower() != name.lower()]
    agents.append({
        "name": name,
        "engine": "codex" if engine == "codex" else "claude",
        "model": model.strip(),
        "systemPrompt": system_prompt.strip()[:MAX_SYSTEM_PROMPT],
    })
    _write_saved_agents(agents)
    return agents


def delete_saved_agent(name: str) -> list[dict]:
    agents = [a for a in load_saved_agents() if a["name"].lower() != name.strip().lower()]
    _write_saved_agents(agents)
    return agents


# --- the prompt / skills library --------------------------------------------
#
# Reusable prompts ("skills"): a slug name, a one-line description and a body,
# saved once and expanded from the composer with `/name`. The same shape as the
# agent library above -- a separate JSON store, atomic writes, name is the key --
# and, like it, deliberately just data: expanding a prompt only fills the
# textarea, it never sends. An opt-in export writes each one to Claude Code's
# own `~/.claude/commands/<name>.md` so `/name` also works in the terminal.

PROMPTS_PATH = Path.home() / ".agentgrid" / "prompts.json"
# The command file lives in Claude Code's own directory; this is the ONE place
# under ~/.claude this feature ever touches, and only ever for files it manages.
CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"
MAX_PROMPT_BODY = 20000
MAX_PROMPT_DESC = 200


def _prompt_slug(name: str) -> str:
    """A slug that is safe both as the store's key and as a command filename.

    Same rules as the session slug -- lowercase, non-alphanumerics folded to
    single hyphens -- so `/name` in the composer, the store key and the
    `<name>.md` on disk all agree on the spelling. Bounded so it cannot become
    a pathological filename.
    """
    text = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return text[:60]


def load_saved_prompts(area_id: str | None = None) -> list[dict]:
    """The library, oldest first. Missing or corrupt file is an empty library."""
    try:
        raw = json.loads(PROMPTS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    prompts = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = _prompt_slug(entry.get("name") or "")
        if not slug:
            continue
        prompts.append({
            "name": slug,
            "description": str(entry.get("description") or "")[:MAX_PROMPT_DESC],
            "body": str(entry.get("body") or "")[:MAX_PROMPT_BODY],
            "areaId": str(entry.get("areaId") or ""),
        })
    if area_id is None or area_id == "*":
        return prompts
    return [p for p in prompts if not p["areaId"] or p["areaId"] == area_id]


def _write_saved_prompts(prompts: list[dict]) -> None:
    PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PROMPTS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(prompts, indent=2), "utf-8")
    os.replace(temporary, PROMPTS_PATH)


def save_saved_prompt(name: str, description: str, body: str, area_id: str = "") -> list[dict]:
    """Save or overwrite one prompt, keyed by slug within a work area."""
    slug = _prompt_slug(name)
    prompts = [p for p in load_saved_prompts("*")
               if not (p["name"] == slug and p.get("areaId", "") == area_id)]
    prompts.append({
        "name": slug,
        "description": description.strip()[:MAX_PROMPT_DESC],
        "body": body.strip()[:MAX_PROMPT_BODY],
        "areaId": area_id,
    })
    _write_saved_prompts(prompts)
    return prompts


def delete_saved_prompt(name: str, area_id: str = "") -> list[dict]:
    slug = _prompt_slug(name)
    prompts = [p for p in load_saved_prompts("*")
               if not (p["name"] == slug and p.get("areaId", "") == area_id)]
    _write_saved_prompts(prompts)
    return prompts


def export_prompt_to_claude(name: str, area_id: str = "") -> tuple[bool, str]:
    """Write one prompt to `~/.claude/commands/<name>.md` as a custom command.

    The file is Claude Code's documented custom-command format: an optional YAML
    frontmatter block carrying the description, then the prompt body. Only ever
    creates or overwrites the single file this prompt owns; nothing else under
    ~/.claude is read, moved or removed. Explicit by design -- there is no code
    path that writes here without the user asking for this prompt by name.
    """
    slug = _prompt_slug(name)
    prompt = next((p for p in load_saved_prompts("*")
                   if p["name"] == slug and p.get("areaId", "") == area_id), None)
    if prompt is None:
        return False, "No such prompt to export."
    front = ""
    description = re.sub(r"\s+", " ", prompt["description"]).strip()
    if description:
        # A double-quoted YAML scalar so a colon or '#' in the description can
        # never break the frontmatter; only backslash and quote need escaping.
        safe = description.replace("\\", "\\\\").replace('"', '\\"')
        front = f'---\ndescription: "{safe}"\n---\n\n'
    body = prompt["body"]
    text = front + body + ("" if body.endswith("\n") else "\n")
    try:
        CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
        path = CLAUDE_COMMANDS_DIR / f"{slug}.md"
        path.write_text(text, "utf-8")
    except OSError as error:
        return False, f"Could not write the command file: {error}"
    return True, str(path)


def agent_env(agent_name: str = "") -> dict:
    """The environment a spawned agent runs in.

    AGENTGRID_AGENT is how `ag ticket` knows the name to file and claim work
    under -- without it an agent would have to be told its own name in the
    prompt and remember it for the whole run. OPENROUTER_API_KEY is stripped
    for the reason it always was: the board's key is the board's, and a
    spawned CLI has no business inheriting it.
    """
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    if agent_name.strip():
        env["AGENTGRID_AGENT"] = agent_name.strip()[:60]
    return env


def spawn_agent(cwd: str, prompt: str, model: str | None,
                allowed: list[dict], engine: str = "claude",
                system_prompt: str = "", interactive: bool = False,
                agent_name: str = "") -> tuple[bool, str, str | None]:
    """Start an agent in a known project.

    Three shapes: a background `claude --bg` daemon (the default, and the only
    one that survives a closed terminal), a detached `codex exec`, or -- when
    `interactive` is set -- a regular `claude` opened in a Terminal tab you can
    watch and type into.

    The prompt is unconstrained -- an agent that could only run vetted prompts
    would be useless -- but the directory must be one of the discovered
    projects. That is the security boundary keeping this endpoint from being
    a general "execute anything anywhere" hole, and it holds for every shape.
    """
    if not prompt.strip():
        return False, "Give the agent something to do.", None
    if cwd not in {project["path"] for project in allowed}:
        return False, "That directory is not one of the known projects.", None
    model = (model or "").strip() or None
    if model and not models.valid(model):
        return False, f'"{model[:60]}" is not a model ID.', None

    # The library's system prompt rides inside the task rather than through a
    # CLI flag: neither `claude --bg` nor `codex exec` documents a
    # system-prompt option that is stable across versions, and a spawn that
    # fails on an unknown flag is worse than a framed preamble.
    if system_prompt.strip():
        prompt = (f"<system instructions>\n{system_prompt.strip()}\n"
                  f"</system instructions>\n\n{prompt}")

    if interactive:
        # Open a real terminal running the CLI, seeded with the task. Both
        # engines have an interactive mode: `claude <prompt>` and `codex
        # <prompt>` each start a watch-and-type-into session.
        return spawn_interactive(cwd, prompt, model, engine, agent_name)

    # A model this machine has no record of may be a typo. Neither detached
    # shape says so at launch, so wait briefly for the refusal.
    check = bool(model) and not models.listed(engine, model)
    on = f" on {model}" if model else ""

    if engine == "codex":
        # codex exec runs the whole task and only then exits, so it cannot be
        # waited on the way `claude --bg` can -- it is detached outright and
        # the rollout file it writes is how the board finds it. There is no
        # job id to hand back; naming waits on (cwd, time) instead.
        argv = ([chat.codex_binary(), "exec", "--cd", cwd, "-s", "workspace-write",
                 "--skip-git-repo-check"]
                + (["-m", model] if model else []) + [prompt])
        # Its output is discarded, except while an unknown model is checked:
        # then stderr goes to an unlinked temp file, which the run keeps
        # writing to and the system frees when it exits.
        log = tempfile.TemporaryFile() if check else None
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log or subprocess.DEVNULL,
                start_new_session=True,
                env=agent_env(agent_name),
            )
            refused = _codex_refusal(proc, log) if log else None
        except FileNotFoundError:
            return False, "codex CLI not found on PATH.", None
        except OSError as error:
            return False, f"could not start codex: {error}", None
        finally:
            if log:
                log.close()
        if refused:
            return False, f"codex stopped straight away: {refused}", None
        return True, f"Started codex in {Path(cwd).name}{on}.", None

    argv = ["claude", "--bg"] + (["--model", model] if model else []) + [prompt]
    try:
        # Output is captured rather than discarded because it is the only
        # handle back to the session just created: `claude --bg` prints the
        # new job id. start_new_session detaches it from this process group.
        done = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            start_new_session=True,
            env=agent_env(agent_name),
        )
    except FileNotFoundError:
        return False, "claude CLI not found on PATH.", None
    except subprocess.TimeoutExpired:
        return False, "claude --bg did not return within 60 seconds.", None
    if done.returncode != 0:
        detail = ANSI_RE.sub("", (done.stderr or done.stdout or "")).strip().splitlines()
        return False, f"claude exited {done.returncode}: {detail[-1][:140] if detail else ''}", None
    plain = ANSI_RE.sub("", done.stdout or "")
    first = plain.splitlines()[0] if plain.splitlines() else ""
    found = JOB_ID_RE.search(first)
    job_id = found.group(1) if found else None
    if check and job_id and _claude_refused(job_id, model, cwd):
        return False, (f"Claude did not accept the model {model}: it may not exist, or this "
                       f"account can't use it. Nothing was started."), None
    return True, f"Started in {Path(cwd).name}{on}.", job_id


# How long a launch with an unknown model waits to be refused. Both CLIs fail
# on their first request, within a few seconds; a model that works just runs.
MODEL_CHECK_SECONDS = 10.0
# Cursor moves in a TUI screen dump: a forward move stands in for spaces.
CURSOR_FORWARD_RE = re.compile(r"\x1b\[\d*C")
TERMINAL_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _codex_refusal(proc, log) -> str | None:
    """Why a detached `codex exec` exited early and unsuccessfully, if it did."""
    deadline = time.monotonic() + MODEL_CHECK_SECONDS
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            if code == 0:
                return None
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 65536))
            return _codex_error(log.read().decode("utf-8", "replace"))
        time.sleep(0.25)
    return None


def _codex_error(text: str) -> str:
    """The API's own message from codex's `ERROR: {json}` line, else its last line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            rest = line[len("ERROR:"):].strip()
            try:
                return str(json.loads(rest)["error"]["message"])[:200]
            except (ValueError, KeyError, TypeError):
                return rest[:200]
    return lines[-1][:200] if lines else "it printed nothing."


def _claude_refused(job_id: str, model: str, cwd: str) -> bool:
    """Whether a background session's first reply is Claude refusing its model.

    `claude --bg` accepts any model and exits 0; the refusal is the session's
    first reply, read here from its screen. A refused session did no work, so
    it is removed rather than left on the board.
    """
    marker = f"issue with the selected model ({model})"
    deadline = time.monotonic() + MODEL_CHECK_SECONDS
    while True:
        try:
            screen = subprocess.run(["claude", "logs", job_id], cwd=cwd, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return False
        plain = TERMINAL_RE.sub("", CURSOR_FORWARD_RE.sub(" ", screen))
        if marker in re.sub(r"[ \t]+", " ", plain):
            try:
                subprocess.run(["claude", "rm", job_id], cwd=cwd, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _osa_str(value: str) -> str:
    """Escape for an AppleScript double-quoted literal.

    Backslash first, then quote -- reversing the order re-escapes the
    backslashes introduced by the quote pass.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


# Terminal.app's scripting dictionary has no command that creates a tab:
# `do script` with no `in` target always spawns a window, and the `tab` class
# it exposes is not creatable via `make`. The only route is to drive the menu
# shortcut through System Events and then aim `do script` at the front window,
# which by then is showing the tab that just opened. The custom-title block is
# try-wrapped because naming is cosmetic and must not take the attach down
# with it.
OPEN_TAB_SCRIPT = '''
tell application "Terminal"
  activate
  if (count of windows) is 0 then
    set theTab to do script "__COMMAND__"
  else
    try
      tell application "System Events" to keystroke "t" using command down
      delay 0.3
      set theTab to do script "__COMMAND__" in front window
    on error
      set theTab to do script "__COMMAND__"
    end try
  end if
  try
    set custom title of theTab to "__TITLE__"
    set title displays custom title of theTab to true
    set title displays device name of theTab to false
    set title displays shell path of theTab to false
    set title displays window size of theTab to false
    set title displays file name of theTab to false
  on error
  end try
  -- Raise Terminal again at the end: the first activate fires before the tab
  -- exists, and on a busy desktop the browser can keep focus -- which read
  -- as "Open did nothing" and taught people to double-click.
  try
    set index of front window to 1
    activate
  end try
end tell
'''


def _open_terminal_tab(command: str, title: str) -> tuple[bool, str]:
    """Open a Terminal.app tab running `command`, titled `title`. macOS only.

    The one place the open-tab AppleScript is actually driven, shared by the
    attach path and the interactive-spawn path: both want "a new tab running
    this shell line", and the only difference is the line. Returns "ok" as the
    success message for the caller to replace with something specific.
    """
    if sys.platform != "darwin":
        return False, f"Terminal.app is macOS-only. Run this yourself: {command}"
    script = (OPEN_TAB_SCRIPT
              .replace("__COMMAND__", _osa_str(command))
              .replace("__TITLE__", _osa_str(title)))
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return False, "osascript not found -- is this really macOS?"
    except subprocess.TimeoutExpired:
        return False, "Terminal.app did not respond within 20 seconds."
    if done.returncode != 0:
        stderr = (done.stderr or "").strip()
        if "-1743" in stderr or "not authorized" in stderr.lower():
            # macOS automation errors are opaque; point at the one switch
            # that fixes this instead of echoing the raw error.
            return False, ("macOS blocked the automation. Allow your terminal under "
                           "System Settings → Privacy & Security → Automation, then retry.")
        return False, f"Could not open Terminal: {stderr[:140]}"
    return True, "ok"


def open_in_terminal(session) -> tuple[bool, str]:
    """Get into a session: focus the existing attach, or open a new tab.

    The refusal order is deliberate: non-background first (resuming an
    interactive session would fork the conversation, which is worse than
    being told no), then a missing job id, then a printed command on
    non-macOS where Terminal.app scripting does not exist.
    """
    if session.kind != "background":
        return False, ("This is an interactive session. Joining it from here would fork "
                       "the conversation with --resume rather than attach to it -- use "
                       "the terminal it is already running in.")
    if not session.job_id:
        return False, "This session has no job id, so there is nothing to attach to."
    command = f'cd "{session.cwd}" && claude attach {session.job_id}'
    if sys.platform != "darwin":
        return False, f"Terminal.app is macOS-only. Run this yourself: {command}"
    existing = terminal.attached_tty(session.job_id)
    if existing:
        ok, _message = terminal.focus_tty(existing)
        if ok:
            return True, "Focused the tab already attached to this session."
        # The tab closed between listing and focusing: fall through and open
        # a fresh one rather than reporting failure.
    ok, message = _open_terminal_tab(command, session.display_title)
    if not ok:
        return False, message
    return True, f"Opened a Terminal tab attached to {session.job_id}."


def spawn_interactive(cwd: str, prompt: str, model: str | None,
                      engine: str = "claude",
                      agent_name: str = "") -> tuple[bool, str, None]:
    """Start a regular, watch-and-type-into CLI session in a fresh Terminal tab.

    The counterpart to a background spawn: rather than detaching a daemon, this
    opens a terminal running an ordinary interactive session, seeded with the
    task. It lives and dies with its tab -- which is the whole reason to pick
    interactive over background. There is no job id to hand back; the board
    finds it the same way it finds any interactive session, from the fleet.

    Both engines have an interactive mode seeded by a positional prompt:
    `claude <prompt>` and `codex <prompt>`. Every piece of the shell line is
    `shlex.quote`d before it is framed into AppleScript, so a prompt full of
    quotes, `$`, or newlines runs as one argument rather than as shell.
    """
    if engine == "codex":
        argv = [chat.codex_binary()] + (["-m", model] if model else []) + [prompt]
        label = "codex"
    else:
        argv = ["claude"] + (["--model", model] if model else []) + [prompt]
        label = "claude"
    # The tab is opened by AppleScript, so the board name rides in as a shell
    # export rather than an env= argument; shlex.quote keeps a name with
    # spaces or quotes in it from becoming shell.
    export = (f"export AGENTGRID_AGENT={shlex.quote(agent_name.strip()[:60])} && "
              if agent_name.strip() else "")
    command = (f"cd {shlex.quote(cwd)} && " + export
               + " ".join(shlex.quote(part) for part in argv))
    if sys.platform != "darwin":
        return False, (f"Starting an interactive session needs Terminal.app (macOS "
                       f"only). Run this yourself: {command}"), None
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else label
    ok, message = _open_terminal_tab(command, first_line[:40] or label)
    if not ok:
        return False, message, None
    return True, (f"Opened an interactive {label} in {Path(cwd).name}"
                  f"{f' on {model}' if model else ''}."), None


# --- @-mention file search ---------------------------------------------------
#
# The chat composer's `@` autocomplete needs the files under a session's cwd,
# ranked against what the user has typed so far. The whole set is listed first
# and only the ranked *result* is capped -- never the pool searched over -- so a
# match is never missed for being past an arbitrary cut-off in the raw list.

MAX_FILE_RESULTS = 50
# A source file past this is not something you read in a side panel; the reader
# caps the bytes it will decode so a stray multi-megabyte blob cannot wedge it.
MAX_FILE_BYTES = 2_000_000
# The file tree renders one DOM row per path; past this a repo is too big to
# draw as a tree, so it is truncated (and the pane says so) rather than hung.
MAX_TREE_FILES = 6000
# The repo path (git ls-files) is unbounded; this bounds only the os.walk
# fallback so a non-repo home directory cannot turn one keystroke into a walk of
# the whole disk. High enough that any ordinary project is listed in full.
WALK_FILE_CAP = 20000


def list_files(cwd: str) -> list[str]:
    """Every path under `cwd`, relative to it, gitignore-aware.

    Inside a git repo `git ls-files` is the source of truth: it already honours
    .gitignore and every nested one, and `--others --exclude-standard` folds in
    files that are new-but-not-ignored, so a file just written shows up before
    it is committed. Run with cwd=cwd, git scopes and relativises to that
    directory for free. Outside a repo -- or if git is missing -- a bounded
    os.walk stands in, skipping .git, node_modules and dotdirs so it does not
    wander into caches. The full set is returned; ranking and the cap happen on
    top of it, never before.
    """
    tracked = _git_files(cwd)
    if tracked is not None:
        return tracked
    return _walk_files(cwd)


def _git_files(cwd: str) -> list[str] | None:
    """Tracked + untracked-but-not-ignored paths, or None when cwd isn't a repo."""
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    paths: list[str] = []
    seen: set[str] = set()
    # Two passes, deduped: committed files first, then untracked-but-not-ignored.
    for extra in ([], ["--others", "--exclude-standard"]):
        try:
            done = subprocess.run(
                ["git", "ls-files", "-z"] + extra,
                cwd=cwd, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            continue
        for rel in done.stdout.split("\0"):
            if rel and rel not in seen:
                seen.add(rel)
                paths.append(rel)
    return paths


def _walk_files(cwd: str) -> list[str]:
    """A bounded, dotdir-skipping walk for directories that are not git repos."""
    root = os.path.abspath(cwd)
    skip = {".git", "node_modules"}
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk never descends into vcs/vendor/dot dirs.
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
            if len(out) >= WALK_FILE_CAP:
                return out
    return out


def _subseq(hay: str, needle: str) -> bool:
    """True when every char of `needle` appears in order within `hay`."""
    it = iter(hay)
    return all(ch in it for ch in needle)


def _file_score(path: str, q: str) -> int | None:
    """A match score for `path` against a lower-cased `q`, or None for no match.

    Higher is better, in tiers: the basename starts with q, the basename
    contains q, the whole path contains q, and last, q is a scattered
    subsequence of the path (so "webpy" still finds "web.py"). Ties break toward
    shorter paths in rank_files, since the shorter one is usually meant.
    """
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    at = base.find(q)
    if at == 0:
        return 1000
    if at > 0:
        return 800 - at
    at = low.find(q)
    if at >= 0:
        return 500 - min(at, 400)
    if _subseq(low, q):
        return 200
    return None


def rank_files(paths: list[str], query: str) -> list[str]:
    """Best matches first for `query` over the full path set.

    An empty query keeps the natural order (git's, or the walk's) so a bare `@`
    still offers something. Otherwise every path is scored and the non-matches
    drop out; shorter paths and a case-folded path break ties.
    """
    q = (query or "").strip().lower()
    if not q:
        return list(paths)
    scored: list[tuple[int, str]] = []
    for path in paths:
        score = _file_score(path, q)
        if score is not None:
            scored.append((score, path))
    scored.sort(key=lambda sp: (-sp[0], len(sp[1]), sp[1].lower()))
    return [path for _score, path in scored]


# --- the HTTP handler --------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Token-gated JSON API plus the single static page.

    `fleet` and `token` are bound onto a per-server subclass by serve(), so
    two instances in one process cannot share state by accident.
    """

    fleet: Fleet
    token: str
    server_version = "agentgrid"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Per-request logging would scroll the launching terminal.
        pass

    # -- plumbing ------------------------------------------------------------

    def _authorized(self, query: dict) -> bool:
        supplied = (query.get("t") or [""])[0] or self.headers.get("X-Agentgrid-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def _cors_origin(self) -> str:
        """The Origin to echo back, but only for the few overlay routes.

        The review overlay runs on the user's dev site (a different origin) and
        must read these responses, so we reflect its Origin. The token still
        gates the request, so reflecting an arbitrary localhost origin only
        matters to a page that already holds the per-run secret.
        """
        origin = self.headers.get("Origin", "")
        path = urllib.parse.urlsplit(self.path).path
        return origin if (origin and path in CORS_PATHS) else ""

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # No embedding, and no referrer leakage of the token in the URL.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            cors = self._cors_origin()
            if cors:
                self.send_header("Access-Control-Allow-Origin", cors)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The browser gave up on the request; nothing useful to do.
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS preflight for the overlay routes; a no-op elsewhere."""
        origin = self.headers.get("Origin", "")
        path = urllib.parse.urlsplit(self.path).path
        self.send_response(204 if (origin and path in CORS_PATHS) else 404)
        if origin and path in CORS_PATHS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agentgrid-Token")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _session_by_id(self, session_id: str):
        for session in self.fleet.raw():
            if session.session_id == session_id:
                return session
        return None

    @staticmethod
    def _one(query: dict, key: str) -> str:
        return (query.get(key) or [""])[0]

    # -- GET -----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's spelling)
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorized(query):
            if parsed.path == "/":
                # A human pasted the bare URL; answer in plain text they can read.
                self._send(403, b"Bad or missing token. Use the URL agentgrid printed.\n",
                           "text/plain; charset=utf-8")
            else:
                self._send_json(403, {"error": "Bad or missing token."})
            return
        route = parsed.path
        if route == "/":
            self._send(200, (STATIC / "app.html").read_bytes(), "text/html; charset=utf-8")
        elif route == "/api/areas":
            self._send_json(200, areas.load())
        elif route == "/overlay.js":
            # The web-review overlay, injected onto the user's own dev site.
            self._send(200, (STATIC / "overlay.js").read_bytes(),
                       "application/javascript; charset=utf-8")
        elif route == "/review":
            # A tiny install page: the draggable bookmarklet and a copyable
            # snippet, with the live token baked in so it just works.
            self._send(200, self._review_install_page().encode("utf-8"),
                       "text/html; charset=utf-8")
        elif route == "/api/events":
            self._events_stream()
        elif route == "/api/providers/openrouter":
            try:
                self._send_json(200, credentials.connection_status())
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
        elif route == "/api/providers/openrouter/models":
            try:
                self._send_json(200, {"models": openrouter.model_catalog()})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
        elif route == "/api/voice":
            self._send_json(200, {"connected": bool(self.voice.key)})
        elif route == "/api/models":
            self._send_json(200, {"models": models.catalog(self.fleet.snapshot().get("sessions", []))})
        elif route == "/api/sessions":
            self._send_json(200, self._sessions_snapshot())
        elif route == "/api/notes":
            self._get_notes(query)
        elif route == "/api/tickets":
            self._get_tickets()
        elif route == "/api/todos":
            self._get_todos(query)
        elif route == "/api/history":
            page = self._one(query, "page")
            self._send_json(200, {"page": page,
                                  "entries": notes.page_history(page),
                                  "jiraBase": notes.jira_base()})
        elif route == "/api/mentions":
            slug = self._one(query, "slug")
            self._send_json(200, {"slug": slug,
                                  "lines": notes.mentions_of(slug),
                                  "jiraBase": notes.jira_base()})
        elif route == "/api/pages":
            self._send_json(200, {"pages": notes.all_pages()})
        elif route == "/api/ping":
            # The cheapest proof this run is alive, for a companion checking
            # the connection file (and for a later run deciding whether to
            # take it over): the pid the file names, and when it started.
            self._send_json(200, {"ok": True, "pid": os.getpid(), "run": self.fleet.events.run,
                                  "started": STARTED_AT})
        elif route == "/api/projects":
            self._send_json(200, {"projects": discover_projects(self.fleet.raw())})
        elif route == "/api/agents":
            self._send_json(200, {"agents": load_saved_agents()})
        elif route == "/api/prompts":
            self._send_json(200, {"prompts": load_saved_prompts(self._one(query, "areaId"))})
        elif route == "/api/sync":
            # The remote and branch to pre-fill the sync sheet with; never any
            # secret, because none is stored -- the transport is the user's git.
            config = sync.load_config()
            self._send_json(200, {
                "remote": config.get("remote", ""),
                "branch": config.get("branch", sync.DEFAULT_BRANCH),
                "lastSync": config.get("last_sync"),
            })
        elif route == "/api/chat/stream":
            self._chat_stream(query)
        elif route == "/api/chat/state":
            self._send_json(200, self.chat.state(self._one(query, "session")))
        elif route == "/api/teams":
            self._send_json(200, {"teams": [teams.team_to_dict(t) for t in teams.list_teams()],
                                  "postures": list(chat.POSTURES),
                                  "runs": {t.name: {"done": r.done, "ok": r.ok}
                                           for t in teams.list_teams() if (r := self.teams.get(t.name))}})
        elif route == "/api/teams/example":
            example = Path(__file__).resolve().parent.parent / "examples" / "teams" / "mlguerrilla-lessons.json"
            self._send_json(200, {"workflow": json.loads(example.read_text(encoding="utf-8"))})
        elif route == "/api/teams/draft":
            draft = self.drafts.status(self._one(query, "id"))
            self._send_json(200 if draft else 404, draft or {"error": "Draft not found."})
        elif route == "/api/teams/stream":
            self._teams_stream(query)
        elif route == "/api/transcript":
            self._get_transcript(query)
        elif route == "/api/files":
            self._get_files(query)
        elif route == "/api/file":
            self._get_file(query)
        elif route == "/api/tree":
            self._get_tree(query)
        else:
            self._send_json(404, {"error": "No such route."})

    def _get_todos(self, query: dict) -> None:
        """Your open todos, for the composer's `[]` picker.

        Same source as the rail (notes.collect): the current window's undone
        checkbox lines plus unfinished ones carried over from before it. Only
        the text and its page travel to the client -- enough to recognise a
        todo and drop its words into a message as context for the agent.
        """
        day = self._one(query, "date") or date.today().isoformat()
        if not _valid_day(day):
            self._send_json(400, {"error": "Bad date."})
            return
        span = self._one(query, "span")
        if span not in ("day", "week", "month"):
            span = "week"
        data = notes.collect(day, span)
        chosen = [t for t in data.get("todos", []) if not t.get("done")]
        chosen += data.get("carried", [])
        todos = [{"text": t["text"], "page": t["page"]}
                 for t in chosen if t.get("text", "").strip()]
        self._send_json(200, {"todos": todos[:200]})

    def _get_notes(self, query: dict) -> None:
        day = self._one(query, "date") or date.today().isoformat()
        if not _valid_day(day):
            self._send_json(400, {"error": "Bad date."})
            return
        span = self._one(query, "span")
        if span not in ("day", "week", "month"):
            span = "day"
        # Materialise recurring pages first, so a pinned page exists before
        # the page list and the fallback below are computed.
        notes.ensure_pinned(day)
        pages = notes.pages(day)
        page = self._one(query, "page")
        if not page or page not in pages:
            # The requested page no longer exists: fall back to the day's
            # first page rather than showing a blank editor for a page that
            # is gone.
            page = pages[0] if pages else notes.DEFAULT_PAGE
        payload = notes.collect(day, span)
        payload.update({
            "date": day,
            "page": page,
            "pages": pages,
            "groups": notes.page_groups(day),
            "collapsed": notes.collapsed_groups(day),
            "pinned": notes.pinned(),
            "text": notes.read_day(day, page),
            "days": notes.days_with_notes(),
        })
        self._send_json(200, payload)

    def _get_transcript(self, query: dict) -> None:
        session = self._session_by_id(self._one(query, "id"))
        if session is None or session.transcript is None:
            self._send_json(200, {"blocks": []})
            return
        path = session.transcript
        agent_id = self._one(query, "agent")
        if agent_id:
            # Resolve the agent against the session's own roster rather than
            # building a path from the request -- the id would otherwise be a
            # path component under the caller's control.
            path = None
            for agent in session.subagents:
                if agent.agent_id == agent_id:
                    path = agent.path
                    break
            if path is None:
                self._send_json(200, {"blocks": []})
                return
        blocks = transcript.render_blocks(path)
        # The tail is the part you opened it to read.
        self._send_json(200, {"blocks": blocks[-600:]})

    def _get_files(self, query: dict) -> None:
        """Fuzzy file-path search under a session's cwd, for @-mention complete.

        The whole tracked file set is searched, then ranked; only the returned
        list is capped for UI sanity. cwd is validated as an existing directory
        -- the composer hands its own open session's project path -- and a bad
        path is a 400 rather than a walk of somewhere unexpected.
        """
        cwd = self._one(query, "cwd")
        if not cwd or not os.path.isdir(cwd):
            self._send_json(400, {"error": "cwd must be an existing directory."})
            return
        q = self._one(query, "q")
        ranked = rank_files(list_files(cwd), q)
        self._send_json(200, {"cwd": cwd, "q": q, "files": ranked[:MAX_FILE_RESULTS]})

    def _get_file(self, query: dict) -> None:
        """One file's text for the read-only code viewer, scoped to a project.

        The path is trusted only when it is a member of list_files(cwd): a
        git ls-files / bounded-walk entry is by construction inside the repo,
        so a crafted `../../etc/passwd` is simply absent from the set and 404s
        -- no separate traversal check needed, and no path built from the
        request ever reaches the filesystem unvalidated. Bytes are capped and
        decoded leniently so a binary that slipped into the set cannot wedge
        the reader.
        """
        cwd = self._one(query, "cwd")
        if not cwd or not os.path.isdir(cwd):
            self._send_json(400, {"error": "cwd must be an existing directory."})
            return
        rel = self._one(query, "path")
        if rel not in set(list_files(cwd)):
            self._send_json(404, {"error": "No such file in this project."})
            return
        try:
            with (Path(cwd) / rel).open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            self._send_json(404, {"error": "File could not be read."})
            return
        truncated = len(data) > MAX_FILE_BYTES
        text = data[:MAX_FILE_BYTES].decode("utf-8", "replace")
        self._send_json(200, {"cwd": cwd, "path": rel, "text": text,
                              "truncated": truncated})

    def _get_tree(self, query: dict) -> None:
        """The whole project's file paths, for the code pane's folder tree.

        Same source as the finder (list_files: git ls-files plus
        untracked-but-not-ignored, or a bounded walk), but uncapped by the
        ranking limit so a tree shows every file -- including the new,
        uncommitted ones that a 50-result search would push off the end.
        """
        cwd = self._one(query, "cwd")
        if not cwd or not os.path.isdir(cwd):
            self._send_json(400, {"error": "cwd must be an existing directory."})
            return
        files = list_files(cwd)
        self._send_json(200, {"cwd": cwd, "files": files[:MAX_TREE_FILES],
                              "truncated": len(files) > MAX_TREE_FILES})

    # -- POST ----------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorized(query):
            self._send_json(403, {"error": "Bad or missing token."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            # Refuse an oversized body without draining it; close the connection
            # so its unread tail can't be misread as the next request.
            self.close_connection = True
            self._send_json(413, {"error": "Request body is too large."})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._send_json(400, {"error": "Bad JSON body."})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Bad JSON body."})
            return
        route = parsed.path
        # Notes routes resolve before any sessionId lookup -- they are not
        # about a session. Every route below has its own explicit branch: the
        # original grouped day-scoped routes into a tuple whose handler chain
        # ended in a bare set_group default, and /api/notes/quickadd listed
        # there silently became "set group" (§7.19). No tuple, no fall-through.
        if route in ("/api/voice/key", "/api/voice/command", "/api/voice/speech", "/api/voice/transcribe"):
            try:
                if route == "/api/voice/key":
                    self.voice.configure(body.get("key", ""))
                    self._send_json(200, {"connected": bool(self.voice.key)})
                elif route == "/api/voice/transcribe":
                    self._send_json(200, self.voice.transcribe(body.get("audio"), body.get("mime")))
                elif route == "/api/voice/command":
                    context = self._sessions_snapshot()
                    context["projects"] = discover_projects(self.fleet.raw())
                    result = self.voice.command(body.get("command", ""), context)
                    action = result.get("action")
                    if action == "spawn":
                        ok, message, job_id = spawn_agent(str(result["cwd"]), str(result["prompt"]), result.get("model") or None,
                                                           context["projects"], str(result.get("engine") or "claude"), "", False)
                        if not ok:
                            raise ValueError(message)
                        result["reply"] = f"Started a {str(result.get('engine') or 'Claude').title()} agent in {next((p.get('name') for p in context['projects'] if p.get('path') == result['cwd']), 'that project')}."
                        result["jobId"] = job_id
                    elif action == "message":
                        session = self._session_by_id(str(result.get("sessionId") or ""))
                        if session is None:
                            raise ValueError("That session disappeared before I could send the message.")
                        self.chat.send(session.session_id, session.cwd, str(result.get("message") or ""), chat.DEFAULT_POSTURE,
                                       engine=session.engine)
                        result["reply"] = f"Message sent to {session.display_title or 'the session'}."
                    self._send_json(200, result)
                else:
                    self._send(200, self.voice.speech(body.get("text"), body.get("voice")), "audio/mpeg")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
        elif route == "/api/areas":
            try:
                self._send_json(200, areas.update(body))
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})
        elif route == "/api/tickets/create":
            self._tickets_create(body)
        elif route == "/api/tickets/update":
            self._tickets_update(body)
        elif route == "/api/tickets/move":
            self._tickets_move(body)
        elif route == "/api/tickets/assign":
            self._tickets_assign(body)
        elif route == "/api/tickets/comment":
            self._tickets_comment(body)
        elif route == "/api/tickets/delete":
            self._tickets_delete(body)
        elif route == "/api/tickets/rekey":
            self._tickets_rekey(body)
        elif route == "/api/notes/save":
            self._notes_save(body)
        elif route == "/api/notes/quickadd":
            self._notes_quickadd(body)
        elif route == "/api/notes/toggle":
            self._notes_toggle(body)
        elif route == "/api/notes/rename":
            self._notes_rename(body)
        elif route == "/api/notes/delete":
            self._notes_delete(body)
        elif route == "/api/notes/order":
            self._notes_order(body)
        elif route == "/api/notes/group":
            self._notes_group(body)
        elif route == "/api/notes/collapse":
            self._notes_collapse(body)
        elif route == "/api/notes/movegroup":
            self._notes_movegroup(body)
        elif route == "/api/notes/pin":
            self._notes_pin(body)
        elif route == "/api/notes/deleteall":
            self._notes_deleteall(body)
        elif route == "/api/notes/renameall":
            self._notes_renameall(body)
        elif route == "/api/spawn":
            self._spawn(body)
        elif route == "/api/review/release":
            self._review_release(body)
        elif route == "/api/projects/add":
            ok, message, can_create = add_project(
                str(body.get("path") or ""), bool(body.get("create")))
            if ok:
                self._send_json(200, {"ok": True, "path": message,
                                      "projects": discover_projects(self.fleet.raw())})
            else:
                self._send_json(400, {"error": message, "canCreate": can_create})
        elif route == "/api/projects/pref":
            path = str(body.get("path") or "")
            if "fav" in body:
                set_project_pref(path, "favorites", bool(body.get("fav")))
            if "hidden" in body:
                set_project_pref(path, "hidden", bool(body.get("hidden")))
            self._send_json(200, {"projects": discover_projects(self.fleet.raw())})
        elif route == "/api/agents/save":
            name = str(body.get("name") or "").strip()
            if not name:
                self._send_json(400, {"error": "A reusable agent needs a name."})
            else:
                self._send_json(200, {"agents": save_saved_agent(
                    name, str(body.get("engine") or "claude"),
                    str(body.get("model") or ""),
                    str(body.get("systemPrompt") or ""))})
        elif route == "/api/agents/delete":
            self._send_json(200, {"agents": delete_saved_agent(str(body.get("name") or ""))})
        elif route == "/api/prompts/save":
            name = str(body.get("name") or "").strip()
            if not _prompt_slug(name):
                self._send_json(400, {"error": "A prompt needs a name with a letter or digit in it."})
            elif not str(body.get("body") or "").strip():
                self._send_json(400, {"error": "A prompt needs a body — the text /name expands to."})
            else:
                self._send_json(200, {"prompts": save_saved_prompt(
                    name, str(body.get("description") or ""),
                    str(body.get("body") or ""), str(body.get("areaId") or ""))})
        elif route == "/api/prompts/delete":
            self._send_json(200, {"prompts": delete_saved_prompt(
                str(body.get("name") or ""), str(body.get("areaId") or ""))})
        elif route == "/api/prompts/export":
            ok, message = export_prompt_to_claude(
                str(body.get("name") or ""), str(body.get("areaId") or ""))
            if ok:
                self._send_json(200, {"ok": True, "path": message})
            else:
                self._send_json(400, {"error": message})
        elif route == "/api/rename":
            self._rename(body)
        elif route == "/api/tags":
            self._tags(body)
        elif route == "/api/read":
            self._read(body)
        elif route == "/api/dismiss":
            self._dismiss(body)
        elif route == "/api/override":
            self._override(body)
        elif route == "/api/open":
            self._open(body)
        elif route == "/api/sync":
            self._sync(body)
        elif route == "/api/chat":
            self._chat_send(body)
        elif route == "/api/chat/upload":
            self._chat_upload(body)
        elif route == "/api/chat/cancel":
            self.chat.cancel(str(body.get("sessionId") or ""))
            self._send_json(200, {"ok": True})
        elif route == "/api/teams":
            self._teams_save(body)
        elif route == "/api/providers/openrouter":
            try:
                action = body.get("action", "save")
                if action == "save":
                    key = str(body.get("key") or "").strip()
                    if not key:
                        raise ValueError("Enter an OpenRouter key.")
                    if os.environ.get("OPENROUTER_API_KEY"):
                        raise ValueError("The server uses OPENROUTER_API_KEY. Remove it from the environment before saving a different key.")
                    openrouter.check_connection(key)
                    credentials.save_key(key)
                    openrouter.clear_cache()
                elif action == "delete":
                    credentials.delete_key()
                    openrouter.clear_cache()
                elif action == "test":
                    openrouter.check_connection()
                else:
                    raise ValueError("Unknown provider action.")
                self._send_json(200, credentials.connection_status())
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
        elif route == "/api/teams/import":
            try:
                team = workflows.parse_workflow(str(body.get("source") or ""))
                self._send_json(200, {"workflow": teams.team_to_dict(team)})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
        elif route == "/api/teams/draft":
            self._workflow_draft(body)
        elif route == "/api/teams/delete":
            self._send_json(200, {"ok": teams.delete_team(str(body.get("name") or ""))})
        elif route == "/api/teams/run":
            self._teams_run(body)
        elif route == "/api/teams/cancel":
            self.teams.cancel(str(body.get("name") or ""))
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "No such route."})

    # -- team routes ---------------------------------------------------------

    def _workflow_draft(self, body: dict) -> None:
        source = str(body.get("source") or "")
        try:
            # JSON export is deterministic and does not require a model call.
            team = workflows.parse_workflow(source)
        except ValueError:
            team = None
        cwd = str(body.get("cwd") or "")
        if team is not None:
            if cwd:
                team.cwd = cwd
            self._send_json(200, {"workflow": teams.team_to_dict(team)})
            return
        if not Path(cwd).is_dir() or not cwd:
            self._send_json(400, {"error": "Choose an existing working directory before building a draft."})
            return
        try:
            job_id = self.drafts.start(source, cwd, str(body.get("engine") or "claude"),
                                       str(body.get("model") or ""))
            self._send_json(202, {"draftId": job_id})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})

    def _teams_save(self, body: dict) -> None:
        # Validate through the same loader the engine uses, so a team saved from
        # the builder is always a team the runner can run.
        try:
            team = teams.load_team(body)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        teams.save_team(team)
        self._send_json(200, {"ok": True})

    def _teams_run(self, body: dict) -> None:
        name = str(body.get("name") or "")
        team = next((t for t in teams.list_teams() if t.name == name), None)
        if team is None:
            self._send_json(404, {"error": "Unknown team. Save it first."})
            return
        if not team.cwd:
            self._send_json(400, {"error": "This team has no working directory set."})
            return
        if any(n.engine == "openrouter" for n in team.nodes) or (team.coordinator or {}).get("engine") == "openrouter":
            try:
                if not credentials.get_key():
                    raise ValueError("Connect OpenRouter in Providers before running this workflow.")
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
        if not Path(team.cwd).is_dir():
            self._send_json(400, {"error": "Working directory does not exist."})
            return
        try:
            self.teams.start(team, str(body.get("input") or ""))
        except ValueError as error:
            self._send_json(409, {"error": str(error)})
            return
        self._send_json(200, {"ok": True})

    def _teams_stream(self, query: dict) -> None:
        """SSE for one team run. Replays the run's log, then streams live events."""
        run = self.teams.get(self._one(query, "name"))
        if run is None:
            self._send_json(404, {"error": "No run for that team yet."})
            return
        channel = run.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(event).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            run.unsubscribe(channel)

    def _events_stream(self) -> None:
        """SSE of session status changes, as they are noticed. Each event is
        `id: <run>-<n>` and a JSON `data:` line; send `Last-Event-ID` when
        reconnecting to get what was missed. A comment every 15 s keeps the
        connection known to be alive."""
        hub = self.fleet.events
        channel = hub.subscribe(self.headers.get("Last-Event-ID", ""))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(f"retry: 3000\n: connected {hub.run}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(event).encode("utf-8")
                self.wfile.write(b"id: " + event["id"].encode("utf-8") + b"\ndata: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(channel)

    def _sessions_snapshot(self) -> dict:
        """The board's fleet, with the chat's own live signal overlaid (see
        _overlay_chat); like any working card, one with a chat turn in flight
        cannot be dragged or re-filed until it settles."""
        snapshot = self.fleet.snapshot()
        _overlay_chat(snapshot.get("sessions", []), self.chat)
        return snapshot

    # -- chat routes ---------------------------------------------------------

    def _chat_send(self, body: dict) -> None:
        # cwd always comes from the tracked session, never the client -- that is
        # the boundary keeping this from being "run claude anywhere".
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        blocked = codex_chat_block(session, self.chat)
        if blocked:
            self._send_json(409, {"error": blocked})
            return
        message = str(body.get("message") or "").strip()
        # Attachments are absolute paths this server minted at /api/chat/upload
        # and are validated back to that directory below, so a message can be a
        # file alone. Only a turn with neither words nor file is empty.
        attachments = self._valid_attachments(body.get("attachments"), session)
        if not message and not attachments:
            self._send_json(400, {"error": "Say something to send."})
            return
        posture = str(body.get("posture") or chat.DEFAULT_POSTURE)
        # Optional per-turn model override (e.g. "sonnet"/"opus"/"haiku"); empty
        # keeps the CLI's configured default. cwd still comes from the session.
        model = str(body.get("model") or "")
        self.chat.send(session.session_id, session.cwd, message, posture, model,
                       attachments, engine=session.engine)
        self._send_json(200, {"ok": True})

    def _valid_attachments(self, raw: object, session) -> list[str]:
        """Keep only paths that are real files inside this session's uploads dir.

        The client sends back the absolute paths /api/chat/upload returned, but
        a path from the client is never trusted on its face: each is resolved
        and must still live under ~/.agentgrid/uploads/<session id> and exist,
        so the send path can never be steered at an arbitrary file on disk.
        """
        if not isinstance(raw, list):
            return []
        try:
            root = session_uploads_dir(session.session_id).resolve()
        except OSError:
            return []
        kept: list[str] = []
        for entry in raw:
            if not isinstance(entry, str) or not entry:
                continue
            try:
                candidate = Path(entry).resolve()
            except OSError:
                continue
            if candidate.parent == root and candidate.is_file():
                kept.append(str(candidate))
        return kept

    def _chat_upload(self, body: dict) -> None:
        """Save a chat attachment to this session's uploads dir; return its path.

        The bytes arrive base64 in a JSON body -- the shape every other POST
        here already uses, so no multipart parser is needed. What the file is
        comes from its bytes (attachment_type), never the client's
        content-type; it is capped at MAX_UPLOAD_BYTES and written under
        ~/.agentgrid/uploads/<session id>/ beside the app's other state. The
        absolute path returned is what a later chat turn hands to the agent.
        Every refusal names the file, so a drop of several reads clearly.
        """
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        label = Path(str(body.get("name") or "")).name[:80] or "That file"
        data = body.get("data")
        if not isinstance(data, str) or not data:
            self._send_json(400, {"error": f"{label} has no data."})
            return
        # Accept a bare base64 string or a full `data:` URL; keep what follows
        # the comma either way.
        if data.startswith("data:"):
            comma = data.find(",")
            data = data[comma + 1:] if comma >= 0 else ""
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            self._send_json(400, {"error": f"{label} did not arrive intact (bad base64)."})
            return
        if not raw:
            self._send_json(400, {"error": f"{label} is empty."})
            return
        if len(raw) > MAX_UPLOAD_BYTES:
            self._send_json(413, {"error": (
                f"{label} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")})
            return
        found = attachment_type(raw, body.get("name"))
        if found is None:
            self._send_json(415, {"error": (
                f"{label} can't be attached. Attach {SUPPORTED_ATTACHMENTS}.")})
            return
        kind, ext = found
        # The random tag makes two files saved in the same second distinct.
        session_dir = session_uploads_dir(session.session_id)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{stamp}-{secrets.token_hex(3)}-{safe_stem(body.get('name'))}.{ext}"
        dest = session_dir / filename
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        except OSError as error:
            self._send_json(500, {"error": f"Could not save {label}: {error}"})
            return
        self._send_json(200, {"path": str(dest), "name": filename,
                              "kind": kind, "bytes": len(raw)})

    def _chat_stream(self, query: dict) -> None:
        """Server-Sent Events for one session's turns. Blocks in its own thread.

        The stream stays open for the life of the panel, delivering every turn's
        events. A subscriber queue is registered for this connection and drained
        here; when the browser goes away the write fails and we unsubscribe. A
        disconnect does NOT cancel the turn -- interrupting a real edit mid-flight
        is worse than letting it finish; the Stop button is the explicit way out.
        """
        session = self._session_by_id(self._one(query, "session"))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        room = self.chat.session(session.session_id, session.cwd, session.engine)
        channel = room.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # defeat proxy buffering
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # heartbeat surfaces a dead client
                    self.wfile.flush()
                    continue
                payload = json.dumps(event).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            room.unsubscribe(channel)

    def _sync(self, body: dict) -> None:
        # Commit-pull-push the notes folder to the configured git remote. The
        # sync module saves the remote/branch before touching the network, so a
        # failure still leaves the sheet pre-filled next time.
        ok, message = sync.sync_notes(
            str(body.get("remote") or ""),
            str(body.get("branch") or sync.DEFAULT_BRANCH),
        )
        if not ok:
            self._send_json(400, {"error": message})
            return
        self._send_json(200, {"ok": True, "message": message,
                              "lastSync": sync.load_config().get("last_sync")})

    # -- notes routes --------------------------------------------------------

    def _day_of(self, body: dict) -> str | None:
        day = body.get("date") or ""
        return day if _valid_day(day) else None

    def _notes_save(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        notes.write_day(day, str(body.get("text") or ""), str(body.get("page") or ""))
        self._send_json(200, {"ok": True})

    def _notes_quickadd(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        text = str(body.get("text") or "").strip()
        if not text:
            self._send_json(400, {"error": "Nothing to add."})
            return
        slug = str(body.get("slug") or "").strip()
        if slug and f"@{slug}" not in text:
            # The panel is a view over the pad; the mention is what ties the
            # line back to the session, so append it if it was not typed.
            text = f"{text} @{slug}"
        if body.get("todo") and not re.match(r"^\s*[-*]\s+\[[ xX]\]", text):
            text = f"- [ ] {text}"
        notes.append_line(day, text)
        self._send_json(200, {"ok": True})

    def _notes_toggle(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        try:
            notes.toggle(day, int(body.get("line") or 0), str(body.get("page") or ""))
        except (IndexError, ValueError):
            # The file changed underneath the rail; the client re-reads.
            self._send_json(409, {"error": "That line is not a checkbox any more."})
            return
        self._send_json(200, {"ok": True})

    def _notes_rename(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        try:
            notes.rename_page(day, str(body.get("page") or ""), str(body.get("name") or ""))
        except (ValueError, FileExistsError, OSError) as error:
            self._send_json(409, {"error": str(error) or "Could not rename."})
            return
        self._send_json(200, {"ok": True})

    def _notes_delete(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        notes.delete_page(day, str(body.get("page") or ""))
        self._send_json(200, {"ok": True})

    def _notes_order(self, body: dict) -> None:
        day = self._day_of(body)
        order = body.get("order")
        if not day or not isinstance(order, list):
            self._send_json(400, {"error": "Bad order."})
            return
        notes.set_order(day, [str(name) for name in order])
        self._send_json(200, {"ok": True})

    def _notes_group(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        notes.set_group(day, str(body.get("page") or ""), str(body.get("group") or ""))
        self._send_json(200, {"ok": True})

    def _notes_collapse(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        notes.set_collapsed(day, str(body.get("group") or ""), bool(body.get("collapsed")))
        self._send_json(200, {"ok": True})

    def _notes_movegroup(self, body: dict) -> None:
        day = self._day_of(body)
        if not day:
            self._send_json(400, {"error": "Bad date."})
            return
        before = body.get("before")
        notes.move_group(day, str(body.get("group") or ""),
                         str(before) if before else None)
        self._send_json(200, {"ok": True})

    def _notes_pin(self, body: dict) -> None:
        page = str(body.get("page") or "").strip()
        if not page:
            self._send_json(400, {"error": "No page named."})
            return
        days = body.get("days")
        notes.set_pinned(page, bool(body.get("pinned")),
                         str(body.get("group") or ""),
                         [int(d) for d in days] if isinstance(days, list) else [])
        self._send_json(200, {"ok": True})

    def _notes_deleteall(self, body: dict) -> None:
        page = str(body.get("page") or "").strip()
        if not page:
            self._send_json(400, {"error": "No page named."})
            return
        self._send_json(200, {"removed": notes.delete_everywhere(page)})

    def _notes_renameall(self, body: dict) -> None:
        page = str(body.get("page") or "").strip()
        name = str(body.get("name") or "").strip()
        if not page or not name:
            self._send_json(400, {"error": "Both names are needed."})
            return
        try:
            notes.rename_everywhere(page, name)
        except (ValueError, FileExistsError, OSError) as error:
            self._send_json(409, {"error": str(error) or "Could not rename."})
            return
        self._send_json(200, {"ok": True})

    # -- session routes ------------------------------------------------------

    # -- tickets -------------------------------------------------------------
    # The board and `ag ticket` write the same files, so everything here is a
    # thin translation: validate, call the module, hand back the whole board.
    # Returning the board rather than only the changed ticket is deliberate --
    # a move re-ranks a column, and a client that patched one card would drift
    # from what the next agent's CLI write already did.

    def _get_tickets(self) -> None:
        board = tickets.board()
        # The live fleet and the known projects ride along, so opening the view
        # costs one request rather than three the client has to join itself.
        board["sessions"] = [
            {"sessionId": session["sessionId"], "title": session["title"],
             "project": session["project"], "cwd": session["cwd"],
             "status": session["status"], "engine": session["engine"]}
            for session in self._sessions_snapshot().get("sessions", [])
        ]
        board["knownProjects"] = discover_projects(self.fleet.raw())
        self._send_json(200, board)

    @staticmethod
    def _ticket_id(body: dict) -> str:
        return str(body.get("id") or body.get("key") or "")

    def _ticket_reply(self, ticket: dict, message: str = "", **extra) -> None:
        payload = {"ok": True, "ticket": ticket, "board": tickets.board()}
        if message:
            payload["message"] = message
        payload.update(extra)
        self._send_json(200, payload)

    @staticmethod
    def _ticket_area(body: dict) -> str:
        """The work area a ticket is being filed under; "" is none."""
        area_id = str(body.get("area") or "")
        if area_id and not any(a["id"] == area_id for a in areas.load()["areas"]):
            raise ValueError("Work area no longer exists.")
        return area_id

    def _tickets_create(self, body: dict) -> None:
        try:
            ticket = tickets.create(
                str(body.get("title") or ""),
                body=str(body.get("body") or body.get("description") or ""),
                type=str(body.get("type") or "task"),
                status=str(body.get("status") or "todo"),
                priority=str(body.get("priority") or "medium"),
                project=str(body.get("project") or ""),
                assignee=str(body.get("assignee") or ""),
                session_id=str(body.get("sessionId") or ""),
                reporter="you",
                labels=body.get("labels"),
                due=str(body.get("due") or ""),
                area=self._ticket_area(body),
            )
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._ticket_reply(ticket, f"Filed {ticket['id']}.")

    def _tickets_update(self, body: dict) -> None:
        fields = {name: body[name] for name in tickets.EDITABLE if name in body}
        if not fields:
            self._send_json(400, {"error": "Nothing to change."})
            return
        try:
            if "area" in fields:
                fields["area"] = self._ticket_area(body)
            ticket = tickets.update(self._ticket_id(body), fields, who="you")
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._ticket_reply(ticket)

    def _tickets_move(self, body: dict) -> None:
        """A drag on the board: a new column, and a place in it.

        `before` is the id of the card it was dropped above; without one it
        lands at the bottom, which is what dropping on the empty space below a
        column means.
        """
        try:
            ticket = tickets.reorder(self._ticket_id(body),
                                     str(body.get("status") or ""),
                                     before=str(body.get("before") or ""),
                                     who="you")
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._ticket_reply(ticket)

    def _tickets_comment(self, body: dict) -> None:
        try:
            ticket = tickets.comment(self._ticket_id(body), "you",
                                     str(body.get("text") or ""))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._ticket_reply(ticket)

    def _tickets_assign(self, body: dict) -> None:
        """Hand a ticket to a live session -- and tell it, in its own chat, so
        the assignment is an instruction rather than a label.

        A bare name with no sessionId is a label only, which is what you want
        for an agent that is not on this board, or is not running yet.
        """
        session_id = str(body.get("sessionId") or "")
        session = self._session_by_id(session_id) if session_id else None
        if session_id and session is None:
            self._send_json(404, {"error": "That session is no longer on the board."})
            return
        assignee = (session.display_title if session
                    else str(body.get("assignee") or "").strip())
        try:
            ticket = tickets.assign(self._ticket_id(body), assignee, who="you",
                                    session_id=session_id,
                                    start=bool(body.get("start")))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        told = False
        if session is not None and body.get("notify", True):
            blocked = codex_chat_block(session, self.chat)
            if blocked:
                message = (f"{ticket['id']} assigned, but {assignee} has not been "
                           f"told yet. {blocked}")
            else:
                self.chat.send(session.session_id, session.cwd,
                               tickets.handoff_prompt(ticket, assignee),
                               chat.DEFAULT_POSTURE, engine=session.engine)
                told = True
                message = f"{ticket['id']} handed to {assignee}."
        elif assignee:
            message = f"{ticket['id']} assigned to {assignee}."
        else:
            message = f"{ticket['id']} unassigned."
        self._ticket_reply(ticket, message, told=told)

    def _tickets_rekey(self, body: dict) -> None:
        try:
            result = tickets.rekey(str(body.get("project") or ""), str(body.get("key") or ""))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        count = result["renamed"]
        message = (f"Prefix is already {result['to']}." if result["from"] == result["to"] else
                   f"{result['from']} is now {result['to']}"
                   f"{f' — {count} ticket' + ('' if count == 1 else 's') + ' renamed' if count else ''}.")
        self._send_json(200, {"ok": True, "message": message, "board": tickets.board(), **result})

    def _tickets_delete(self, body: dict) -> None:
        ticket_id = self._ticket_id(body)
        try:
            tickets.get(ticket_id)    # a bad or unknown id says so; nothing is unlinked
        except ValueError as error:
            self._send_json(404, {"error": str(error)})
            return
        tickets.delete(ticket_id)
        self._send_json(200, {"ok": True, "board": tickets.board()})

    def _spawn(self, body: dict) -> None:
        area_id = str(body.get("areaId") or "")
        if area_id and not any(a["id"] == area_id for a in areas.load()["areas"]):
            self._send_json(400, {"error": "Work area no longer exists."})
            return
        started = time.time()
        known = [s.session_id for s in self.fleet.raw()]
        projects = discover_projects(self.fleet.raw())
        engine = "codex" if str(body.get("engine") or "") == "codex" else "claude"
        interactive = bool(body.get("interactive"))
        name = str(body.get("name") or "").strip()
        prompt = str(body.get("prompt") or "")
        # An agent started for a ticket gets the ticket as its brief, with the
        # exact commands to report back, ahead of anything typed.
        ticket = None
        agent_label = ""
        ticket_id = str(body.get("ticketKey") or body.get("ticketId") or "").strip()
        if ticket_id:
            try:
                ticket = tickets.get(ticket_id)
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            agent_label = name or f"{ticket['id']} · {engine}"
            handoff = tickets.handoff_prompt(ticket, agent_label)
            prompt = f"{handoff}\n\n{prompt.strip()}" if prompt.strip() else handoff
        if body.get("noProject"):
            # A project-less agent, like running `claude` from the Desktop. The
            # cwd is resolved here rather than trusted from the client, and only
            # this one server-chosen root is added to the allowlist, so the
            # spawn boundary still holds.
            root = freeform_root()
            cwd = str(root)
            projects = projects + [{"path": cwd, "name": root.name, "label": root.name}]
        else:
            cwd = str(body.get("cwd") or "")
        ok, message, job_id = spawn_agent(
            cwd,
            prompt,
            str(body.get("model") or "") or None,
            projects,
            engine,
            str(body.get("systemPrompt") or ""),
            interactive,
            agent_label or name,
        )
        if not ok:
            self._send_json(400, {"error": message})
            return
        if area_id:
            areas.await_session(area_id, cwd, engine, interactive, job_id, known, started)
        if ticket is not None:
            try:
                tickets.assign(ticket["id"], agent_label, who="you", start=True)
                message += f" {ticket['id']} is in progress."
            except ValueError as error:
                message += f" Could not assign {ticket['id']}: {error}"
        if name:
            if job_id:
                self.fleet.name_when_seen(job_id, name)
            elif engine == "codex":
                # A codex session (exec or interactive) is found in the rollout
                # files by cwd + time, never a job id.
                self.fleet.name_codex_when_seen(cwd, name)
            elif interactive:
                self.fleet.name_interactive_when_seen(cwd, name)
            else:
                # Say the name was not applied rather than dropping it silently.
                message += " Could not read its id, so the name was not applied."
        self._send_json(200, {"ok": True, "message": message})

    # -- web-review overlay --------------------------------------------------

    def _review_release(self, body: dict) -> None:
        """Take a batch of overlay comments and hand it to an agent.

        With a running session chosen, the compiled instruction is queued onto
        that session's chat (the loop the user wants: annotate the live app,
        release, the same agent fixes it). With no target ("New agent"), the
        batch is parked on disk so nothing is lost before it is picked up.
        """
        raw = body.get("comments")
        comments = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        if not comments:
            self._send_json(400, {"error": "No comments to release."})
            return
        url = str(body.get("url") or "")[:2000]
        title = str(body.get("title") or "")[:300]
        target = str(body.get("target") or "").strip()
        if target:
            session = self._session_by_id(target)
            if session is None:
                self._send_json(404, {"error": "That session is no longer running. Pick another, or New agent."})
                return
            # Marked-up screenshots are saved into this session's uploads dir and
            # attached to the turn, so the agent sees the drawing, not just reads
            # about it.
            img_dir = session_uploads_dir(session.session_id)
            attachments = []
            for c in comments:
                saved = self._save_review_image(img_dir, c.get("image"))
                if saved:
                    c["_imgpath"] = saved
                    attachments.append(saved)
            prompt = _compile_review_prompt(url, title, comments)
            self.chat.send(session.session_id, session.cwd, prompt,
                           chat.DEFAULT_POSTURE, "", attachments, engine=session.engine)
            self._send_json(200, {"ok": True, "dispatched": "session",
                                  "sessionId": session.session_id,
                                  "attached": len(attachments)})
            return
        # Parked: save any screenshots beside the batch and reference their paths
        # in the prompt, so a later pickup still has the drawings.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        img_dir = REVIEWS_DIR / stamp
        for c in comments:
            saved = self._save_review_image(img_dir, c.get("image"))
            if saved:
                c["_imgpath"] = saved
        prompt = _compile_review_prompt(url, title, comments)
        path = _save_pending_review(url, title, prompt, comments, stamp)
        self._send_json(200, {"ok": True, "dispatched": "pending", "path": str(path)})

    def _save_review_image(self, dir_path: Path, data: object) -> str | None:
        """Save one inline screenshot (base64/`data:` URL) and return its path.

        Returns None for a missing or invalid image rather than failing the
        whole release -- a bad drawing should never lose the words that came
        with it.
        """
        if not isinstance(data, str) or not data:
            return None
        try:
            raw, ext = decode_image_data(data)
        except (ValueError, binascii.Error):
            return None
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            dest = dir_path / f"review-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.{ext}"
            dest.write_bytes(raw)
        except OSError:
            return None
        return str(dest)

    def _review_install_page(self) -> str:
        """The /review page: drag-to-install bookmarklet + copy snippet."""
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        # The bookmarklet injects overlay.js with the origin+token baked in.
        # json.dumps keeps the token safely quoted inside the javascript: URL.
        loader = (
            "javascript:(function(){var o=%s,t=%s,d=document,"
            "s=d.createElement('script');s.src=o+'/overlay.js?ag='+"
            "encodeURIComponent(o)+'&t='+encodeURIComponent(t)+'&_='+Date.now();"
            "(d.body||d.documentElement).appendChild(s);})();"
            % (json.dumps(origin), json.dumps(self.token))
        )
        href = html.escape(loader, quote=True)
        snippet = html.escape(loader[len("javascript:"):])
        return _REVIEW_PAGE.replace("__HREF__", href).replace("__SNIPPET__", snippet)

    def _rename(self, body: dict) -> None:
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        name = str(body.get("name") or "").strip()
        discovery.save_custom_name(session.session_id, name)
        session.custom_name = name or None
        self._send_json(200, {"ok": True})

    def _tags(self, body: dict) -> None:
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        raw = body.get("tags")
        cleaned = discovery.normalize_tags(raw if isinstance(raw, list) else [])
        discovery.save_tags(session.session_id, cleaned)
        session.tags = cleaned
        self._send_json(200, {"tags": cleaned})

    def _read(self, body: dict) -> None:
        # One route, both directions: the client says what it wants the
        # state to be. Marking unread forgets the read position rather than
        # setting a flag -- unread is "have you seen this far", and the honest
        # way to say no is to have no record.
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        if body.get("unread"):
            discovery.mark_unread(session.session_id)
            session.unread = True
        else:
            discovery.mark_read(session.session_id, session.last_activity)
            session.unread = False
        self._send_json(200, {"ok": True})

    def _dismiss(self, body: dict) -> None:
        # One route, both directions, like /api/read: the client asks to clear
        # a card or to bring it back. A dismissal is a position (the reply you
        # cleared), so a live session that speaks again returns on its own and
        # the board can never lose a session that is actually still talking.
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        if body.get("undo"):
            discovery.restore_session(session.session_id)
        else:
            discovery.dismiss_session(session.session_id, session.last_activity)
        self._send_json(200, {"ok": True})

    def _override(self, body: dict) -> None:
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        to = body.get("to")
        if to in ("", None, "auto"):
            to = None
        # base and at come from the server's view of reality, never from the
        # client: the card the user dragged may already be showing an
        # override, and re-basing onto that pins the entry to a state that
        # can never expire.
        ok, message = discovery.save_override(
            session.session_id, to, session.real_status, session.last_activity)
        if not ok:
            self._send_json(409, {"error": message})
            return
        self._send_json(200, {"ok": True, "message": message})

    def _open(self, body: dict) -> None:
        session = self._session_by_id(str(body.get("sessionId") or ""))
        if session is None:
            self._send_json(404, {"error": "Unknown session."})
            return
        # Two different verbs share this route. A background session is joined
        # (attach, or focus the tab already attached); an interactive session
        # cannot be joined -- its keyboard belongs to the pty inside whatever
        # emulator owns it -- but a Terminal.app tab can at least be brought to
        # the front. focus() explains itself for hosts it cannot script, which
        # is the honest half of "get into a session" for Cursor-hosted ones.
        if session.kind == "background":
            ok, message = open_in_terminal(session)
        elif session.pid:
            ok, message = terminal.focus(session.pid)
        else:
            ok, message = False, ("This interactive session reports no pid, so its "
                                  "terminal tab cannot be found.")
        if not ok:
            self._send_json(409, {"error": message})
            return
        self._send_json(200, {"ok": True, "message": message})


# --- web-review helpers ------------------------------------------------------


def _compile_review_prompt(url: str, title: str, comments: list) -> str:
    """Turn a batch of overlay comments into one clear instruction for an agent.

    Each comment carries where it was left (a text selection, an image, or a
    clicked element) and what to change. The location goes first so the agent
    can find the spot, then the requested change.
    """
    head = title.strip() or url or "the running app"
    lines = [
        f"I reviewed {head} in the browser and left the edits below. "
        "Please make each change in the code, then tell me what you changed.",
        "",
        f"Page: {url}" if url else "",
        "",
    ]
    for i, c in enumerate(comments, 1):
        kind = str(c.get("kind") or "point")
        quote = str(c.get("quote") or "").strip()[:600]
        label = str(c.get("label") or "").strip()[:300]
        selector = str(c.get("selector") or "").strip()[:300]
        note = str(c.get("note") or "").strip()[:2000]
        if kind == "text":
            where = f'selected text: "{quote}"' if quote else "a selected span of text"
        elif kind == "image":
            where = f"the image ({label})" if label else "an image"
        else:
            where = f"the element `{selector}`" if selector else "a spot on the page"
        lines.append(f"{i}. On {where}:")
        lines.append(f"   {note}")
        # A marked-up screenshot rides with this item when the reviewer drew on
        # one; name the file so the agent can open it (it is also attached to
        # the turn when this goes to a running session).
        imgpath = str(c.get("_imgpath") or "").strip()
        if imgpath:
            lines.append(f"   Marked-up screenshot (my drawing shows the spot): {imgpath}")
        lines.append("")
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def _save_pending_review(url: str, title: str, prompt: str, comments: list,
                         stamp: str | None = None) -> Path:
    """Park a released batch with no chosen session, so it is never lost.

    Inline screenshot bytes are dropped from the stored JSON -- they are already
    written as files next to it and referenced by `_imgpath` -- so the record
    stays small.
    """
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = REVIEWS_DIR / f"{stamp}.json"
    lean = [{k: v for k, v in c.items() if k != "image"} for c in comments]
    path.write_text(json.dumps({
        "createdAt": time.time(),
        "url": url,
        "title": title,
        "prompt": prompt,
        "comments": lean,
    }, indent=2), encoding="utf-8")
    return path


_REVIEW_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentGrid · Web Review</title>
<style>
:root{--bg:#fbfbfa;--card:#fff;--text:#18181b;--text-2:#6b6b73;--line-2:#d6d6d2;
--accent:#b4690e;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#131315;--card:#1a1a1d;--text:#ececee;
--text-2:#9797a0;--line-2:#33333a;--accent:#d9a441}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);
line-height:1.55;display:flex;justify-content:center;padding:48px 20px}
main{max-width:640px;width:100%}
h1{font-size:22px;margin:0 0 6px}
p.sub{color:var(--text-2);margin:0 0 28px;font-size:14px}
.step{background:var(--card);border:1px solid var(--line-2);border-radius:14px;
padding:18px 20px;margin-bottom:16px}
.step h2{font-size:14px;margin:0 0 10px;display:flex;gap:8px;align-items:center}
.step h2 .n{width:22px;height:22px;border-radius:50%;background:var(--accent);
color:#fff;font-size:12px;font-weight:700;display:flex;align-items:center;
justify-content:center}
@media(prefers-color-scheme:dark){.step h2 .n{color:#131315}}
.bm{display:inline-flex;align-items:center;gap:8px;height:40px;padding:0 18px;
background:var(--accent);color:#fff;border-radius:20px;text-decoration:none;
font-weight:640;font-size:14px;cursor:grab}
@media(prefers-color-scheme:dark){.bm{color:#131315}}
.small{color:var(--text-2);font-size:13px;margin-top:10px}
code{font-family:var(--mono);font-size:12px}
pre{background:var(--bg);border:1px solid var(--line-2);border-radius:10px;
padding:12px;overflow:auto;font-family:var(--mono);font-size:11.5px;
white-space:pre-wrap;word-break:break-all;margin:0}
button.copy{margin-top:10px;height:32px;padding:0 14px;border-radius:8px;
border:1px solid var(--line-2);background:var(--card);color:var(--text);
font-size:13px;font-weight:600;cursor:pointer;font-family:var(--sans)}
ul{margin:6px 0 0;padding-left:20px;color:var(--text-2);font-size:13px}
li{margin:3px 0}
</style></head><body><main>
<h1>Web Review overlay</h1>
<p class="sub">Comment on any localhost page you're building, then release the
edits to an agent to fix.</p>
<div class="step"><h2><span class="n">1</span> Install the bookmarklet</h2>
<p style="margin:0 0 12px;font-size:14px">Drag this button up to your bookmarks
bar:</p>
<a class="bm" href="__HREF__">\U0001f4ac Review this page</a>
<p class="small">Can't drag it? Copy the snippet below and paste it into the
browser's DevTools Console on the page you want to review.</p>
<pre id="snip">__SNIPPET__</pre>
<button class="copy" onclick="navigator.clipboard.writeText(document.getElementById('snip').textContent);this.textContent='Copied ✔'">Copy snippet</button>
</div>
<div class="step"><h2><span class="n">2</span> Open your app and click it</h2>
<p style="margin:0;font-size:14px">Go to the page you're building (e.g.
<code>localhost:3000</code>) and click the bookmarklet. A <b>Review</b> pill
appears bottom-right. Press <code>c</code> to arm it.</p></div>
<div class="step"><h2><span class="n">3</span> Comment, then release</h2>
<ul>
<li><b>Highlight text</b> → a Comment chip appears → type your note.</li>
<li><b>Click near anything</b> (including an image) → a pin drops → type your note.</li>
<li>Open the tray, pick the agent working on this project, and hit
<b>Release</b>. The agent gets every edit as one instruction.</li>
</ul></div>
<p class="small">The token in this bookmarklet is tied to this AgentGrid run.
If you restart AgentGrid, revisit this page and re-install it.</p>
</main></body></html>"""


# --- entry point -------------------------------------------------------------

# Where a local companion (Chief of Staff) finds this run: the port and the token
# the printed URL carries. Owner-only. A run claims it on start and keeps it
# claimed for as long as it runs (see keep_connection_file); it removes the
# file on a clean stop only if the file is still its own.
CONNECTION_FILE = Path.home() / ".agentgrid" / "web.json"
CONNECTION_KEEP_SECONDS = 5.0
STARTED_AT = datetime.now().isoformat(timespec="seconds")


def write_connection_file(port: int, token: str, path: Path = CONNECTION_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"url": f"http://127.0.0.1:{port}", "port": port, "token": token,
                       "pid": os.getpid(), "started": STARTED_AT}, handle)
        os.replace(tmp, path)
    except OSError:
        pass  # the printed URL still works; only the companion loses its way in


def read_connection_file(path: Path = CONNECTION_FILE) -> dict | None:
    """The file's record, or None when it is missing or not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def connection_answers(record: dict, timeout: float = 1.5) -> bool:
    """Whether the run a connection record names is alive and answers with its token.

    The pid check alone is not enough: a run that died without cleaning up
    leaves its pid to be reused, and a run that was killed mid-verification
    leaves a file that names nothing. Only an answer to /api/ping that
    accepts the token counts.
    """
    pid, url, token = record.get("pid"), record.get("url"), record.get("token")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass  # not ours to signal, but it exists: let the request decide
    if not isinstance(url, str) or not isinstance(token, str) or not token:
        return False
    try:
        request = urllib.request.Request(f"{url}/api/ping", headers={"X-Agentgrid-Token": token})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        # 403 is the token being refused: whatever listens there is not that
        # run. Anything else (a 404 from a run older than /api/ping) is a run
        # that accepted the token, so it is alive.
        return error.code != 403
    except (OSError, ValueError):
        return False


def claim_connection_file(port: int, token: str, path: Path = CONNECTION_FILE,
                          answers=connection_answers) -> bool:
    """Make the file name this run, unless another run that still answers holds it.

    Returns whether the file names this run afterwards. A missing file, a
    garbled one, or one left behind by a run that no longer answers is taken
    over; one held by a live run is left alone, so a verification run on a
    spare port never steals the companion's way in, and the run that is still
    there is the one it keeps talking to.
    """
    record = read_connection_file(path)
    if record is not None:
        if record.get("token") == token:
            return True
        if record.get("pid") != os.getpid() and answers(record):
            return False
    write_connection_file(port, token, path)
    return (read_connection_file(path) or {}).get("token") == token


def keep_connection_file(port: int, token: str, stop: threading.Event,
                         path: Path = CONNECTION_FILE, every: float = CONNECTION_KEEP_SECONDS) -> None:
    """Re-claim the file for as long as this run lives.

    Anything can take the file away while a run is up: a second `ag --web`
    (a verification run, a second board) overwrites it and removes it when it
    stops, or dies and leaves it naming a pid that is gone. Either way the
    companion says agentgrid is not running while it plainly is. Checking
    every few seconds costs one stat, and a probe only when someone else
    holds the file.
    """
    while not stop.wait(every):
        try:
            claim_connection_file(port, token, path)
        except Exception:  # noqa: BLE001 -- the board must outlive any fault here
            pass


def remove_connection_file(token: str, path: Path = CONNECTION_FILE) -> None:
    """Remove the file only if it is still ours, so a newer run's file survives."""
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
            path.unlink()
    except (OSError, ValueError):
        pass


def serve(port: int = 8787, open_browser: bool = True,
          roots: list[str] | None = None) -> None:
    """Start the poller and the HTTP server, print the one URL that gets in."""
    if not (STATIC / "app.html").is_file():
        raise SystemExit("agentgrid/static/app.html is missing; the web UI cannot start.")
    scanning = configure_roots(roots)
    fleet = Fleet()
    fleet.start()
    token = secrets.token_urlsafe(24)
    # Bind fleet, token and the chat manager onto a per-server subclass rather
    # than globals, so two servers in one process cannot share state by accident.
    handler = type("BoundHandler", (Handler,),
                   {"fleet": fleet, "token": token, "chat": chat.ChatManager(),
                    "teams": teams.TeamManager(), "drafts": workflows.DraftManager(), "voice": voice.Voice()})
    # The events the fleet announces show the chat's live signal too, as /api/sessions does.
    fleet.overlay = lambda sessions: _overlay_chat(sessions, handler.chat)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # Address already in use: a second instance starts on a free port
        # instead of dying.
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    claim_connection_file(httpd.server_address[1], token)
    keeping = threading.Event()
    keeper = threading.Thread(target=keep_connection_file, args=(httpd.server_address[1], token, keeping),
                              daemon=True, name="connection-file")
    keeper.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={token}"
    review_url = f"http://127.0.0.1:{httpd.server_address[1]}/review?t={token}"
    print("agentgrid web UI")
    print(f"  {url}")
    print(f"  web review overlay: {review_url}")
    print("  projects from: " + (", ".join(str(root) for root in scanning) or "(no roots found)"))
    # flush=True because Python block-buffers stdout when it is not a
    # terminal, and `ag --web > log &` must still show the URL that carries
    # the token -- it is the only way in.
    print("  loopback only, token-gated, stops when you Ctrl-C this terminal.",
          flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        keeping.set()
        keeper.join(timeout=3)  # never let a claim in flight put the file back after it is removed
        remove_connection_file(token)
        fleet.stop()
        httpd.server_close()
