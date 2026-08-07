"""Session discovery and the whole state model.

This module is the data layer both front ends sit on. It asks the claude CLI
for the fleet, enriches each session from its transcript on disk, and folds in
four small JSON files of human judgement: names, tags, manual overrides and
read positions. Everything a card displays comes out of `collect()`.

The design decision worth keeping: there are two sources and only two. The
CLI's JSON says what exists and what state the daemon believes each session is
in; the transcript says what actually happened and when. Nothing is scraped
from a rendered screen, ever — every fact here comes from an interface Claude
Code already exposes, which is what keeps this safe to run against live
sessions.

Two vocabularies arrive from the CLI and they are collapsed in exactly one
place (`normalize_status`). Background sessions report `state`
(working/blocked/done/failed/stopped); interactive sessions report `status`
(busy/idle). Letting that difference leak past this module means every
consumer has to know about it, so none of them do.

A second decision: `collect()` runs on a two-second poll and must never write
files. Stale overrides are ignored rather than deleted — `save_override`
rewrites the whole map on the next deliberate change anyway, so they never
accumulate for long, and a poll that writes is a poll that can corrupt.

Every state file here is optional enrichment. A missing or corrupt file reads
as `{}` and the app keeps running; a half-written file can never be observed
because every write goes through a temp file and `os.replace`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
JOBS_DIR = Path.home() / ".claude" / "jobs"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_INDEX_PATH = Path.home() / ".codex" / "session_index.jsonl"
HOOK_STATE_PATH = Path.home() / ".agentgrid" / "state.json"
NAMES_PATH = Path.home() / ".agentgrid" / "names.json"
OVERRIDES_PATH = Path.home() / ".agentgrid" / "overrides.json"
TAGS_PATH = Path.home() / ".agentgrid" / "tags.json"
READ_PATH = Path.home() / ".agentgrid" / "read.json"
DISMISSED_PATH = Path.home() / ".agentgrid" / "dismissed.json"

MAX_TAGS_PER_SESSION = 6
MAX_TAG_LENGTH = 24
MAX_NAME_LENGTH = 60

# `complete` exists only in agentgrid, never in the CLI. It is the terminal
# state that only a human can set — the CLI's `done` means "the turn ended",
# which is a weaker claim than "the work is finished".
COMPLETE = "complete"
OVERRIDABLE = ("blocked", "done", "idle", COMPLETE)

# An interactive session that went quiet inside this window with unread
# output reads as "replied", not "idle" -- see collect().
RECENT_REPLY_SECONDS = 3600

# An interactive session you have talked to holds its Replied slot even after
# you have read the answer -- opening a reply is not the same as being done
# with the conversation. It only falls to Idle once it has gone quiet (no new
# activity in the transcript) for this long. See collect().
INTERACTIVE_REPLY_WINDOW = 48 * 3600

# An interactive session drops off `claude agents` the instant its terminal
# closes; its transcript is read back from disk for this long so a closed tab
# lingers as a card you can still find and resume rather than vanishing. Held
# to the same 24h window Codex uses, for one shared idea of "recently active".
ENDED_INTERACTIVE_WINDOW = 24 * 3600

# Grouped by how likely you are to act, then most-recent within the band.
# A session blocked on a prompt for an hour outranks one you opened a minute
# ago, so this is deliberately not a plain recently-active list.
STATUS_ORDER = {
    "blocked": 0,
    "working": 1,
    "failed": 2,
    "done": 3,
    "idle": 4,
    "complete": 5,
    "stopped": 6,
    "unknown": 7,
}


@dataclass
class SubAgent:
    """One subagent transcript joined back to its parent session."""

    agent_id: str
    description: str
    agent_type: str
    status: str  # "working" | "done"
    path: Path | None = None
    messages: int = 0


@dataclass
class Session:
    """Everything known about one Claude Code session, from both sources.

    `job_id` is NOT interchangeable with `session_id`. The UUID names the
    transcript; the short job handle is what `claude attach` takes, and only
    background sessions have one. They look confusingly alike — the UUID
    begins with the same eight characters — which is exactly why they are
    separate fields.
    """

    session_id: str
    kind: str
    status: str
    cwd: str
    started_at: int  # epoch milliseconds, as the CLI reports it
    engine: str = "claude"  # "claude" | "codex" — which CLI owns the session
    name: str | None = None
    pid: int | None = None
    job_id: str | None = None
    custom_name: str | None = None
    title: str | None = None
    last_prompt: str | None = None
    git_branch: str | None = None
    model: str | None = None
    subagents: list[SubAgent] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    transcript: Path | None = None
    last_activity: float = 0.0
    overridden: bool = False
    real_status: str = ""
    unread: bool = True
    tags: list[str] = field(default_factory=list)

    @property
    def project(self) -> str:
        """The directory name the session runs in, or the raw cwd."""
        return Path(self.cwd).name or self.cwd

    @property
    def display_title(self) -> str:
        """The four-source naming fallback, in order of how much a human meant it.

        A name you typed beats the AI's guess, which beats the CLI's
        auto-generated name, which beats the first eight characters of the id.
        """
        return (
            self.custom_name
            or self.title
            or self.name
            or self.session_id[:8]
        )

    @property
    def sort_key(self) -> tuple[int, float]:
        return (STATUS_ORDER.get(self.status, 9), -self.last_activity)

    def age_seconds(self) -> float:
        return time.time() - self.started_at / 1000

    def idle_seconds(self) -> float:
        """Seconds since anything happened, falling back to the session's age.

        A session with no transcript has no activity to measure, and "as old
        as it is" is the honest answer rather than zero.
        """
        if self.last_activity:
            return time.time() - self.last_activity
        return self.age_seconds()


# ---------------------------------------------------------------------------
# JSON state files. Every read tolerates absence and corruption; every write
# is atomic. These are optional enrichments, and a corrupt file must not take
# the app down.


def _read_json(path: Path) -> dict:
    """Read a state file, returning {} on any failure.

    Missing, unreadable and malformed all collapse to the same answer because
    the caller treats all three identically: proceed without the enrichment.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    """Write a state file atomically.

    Write to `*.json.tmp`, then `os.replace`. A concurrent reader — the other
    front end, or the hook — must never see a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# The fleet, from the CLI.


def normalize_status(raw: dict) -> str:
    """Collapse the CLI's status vocabularies into one.

    `state` wins if present: only background sessions have it, and it is the
    richer vocabulary. Interactive sessions report busy / idle / waiting.
    `waiting` — a pending question or permission prompt — was caught live by
    sampling while a question blocked a session; unmapped it matched no
    column, so the card vanished from the board at exactly the moment it
    most needed attention. It is the interactive `blocked`, straight through.
    """
    state = raw.get("state")
    if state:
        return {
            "working": "working",
            "blocked": "blocked",
            "done": "done",
            "failed": "failed",
            "stopped": "stopped",
        }.get(state, state)
    return {"busy": "working", "idle": "idle", "waiting": "blocked"}.get(
        raw.get("status"), raw.get("status") or "unknown"
    )


def list_sessions(timeout: int = 10) -> tuple[list[Session], str | None]:
    """Ask the claude CLI for the fleet. Never raises.

    Each failure mode gets its own message, because "something went wrong" is
    not diagnosable from a dashboard: a missing binary, a hang, a non-zero
    exit and unparseable output are four different problems with four
    different fixes.
    """
    try:
        done = subprocess.run(
            ["claude", "agents", "--json", "--all"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return [], "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return [], f"claude agents timed out after {timeout}s"
    except OSError as error:
        return [], f"could not run claude agents: {error}"

    if done.returncode != 0:
        detail = (done.stderr or "").strip()[:120]
        return [], f"claude agents exited {done.returncode}: {detail}"

    try:
        raw_sessions = json.loads(done.stdout or "[]")
    except ValueError:
        return [], "claude agents returned output that is not JSON"
    if not isinstance(raw_sessions, list):
        return [], "claude agents returned JSON that is not a list"

    sessions: list[Session] = []
    for raw in raw_sessions:
        if not isinstance(raw, dict):
            continue
        session_id = raw.get("sessionId") or ""
        if not session_id:
            continue
        kind = raw.get("kind") or "interactive"
        sessions.append(
            Session(
                session_id=session_id,
                kind=kind,
                status=normalize_status(raw),
                cwd=raw.get("cwd") or "",
                started_at=int(raw.get("startedAt") or 0),
                name=raw.get("name"),
                pid=raw.get("pid"),
                # The short job handle exists for background sessions only,
                # and it is the only thing `claude attach` accepts — passing
                # the UUID fails with "No job matching <uuid>".
                job_id=raw.get("id") if kind == "background" else None,
            )
        )
    return sessions, None


def job_tempo(job_id: str | None) -> str | None:
    """Read the daemon's own activity signal for a background job.

    Neither field the CLI reports answers "is it working right now": `state`
    can stick at working after a job's turn has ended, and `status` says busy
    whenever any task is in flight — a stray local_bash winding down after the
    turn ended keeps a finished job in Working. The daemon already tracks the
    real answer as `tempo` (active / idle) in ~/.claude/jobs/<id>/state.json;
    the CLI just does not pass it through, so it is read from disk here.
    """
    if not job_id:
        return None
    try:
        raw = json.loads((JOBS_DIR / job_id / "state.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    tempo = raw.get("tempo") if isinstance(raw, dict) else None
    return tempo if tempo in ("active", "idle") else None


def apply_tempo(session: Session) -> Session:
    """Let the daemon's tempo decide working-or-not for background sessions.

    `blocked`, `failed` and `stopped` are never overridden — those are claims
    tempo cannot make. Interactive sessions have no job directory, so the old
    heuristic (the CLI's own fields) stays as the fallback for them, and for
    any job whose state file is missing or unreadable.
    """
    if session.kind != "background" or session.status in ("blocked", "failed", "stopped"):
        return session
    tempo = job_tempo(session.job_id)
    if tempo == "active":
        session.status = "working"
    elif tempo == "idle" and session.status == "working":
        # The turn ended but `state` never moved on: this is a reply to read,
        # not a job still running.
        session.status = "done"
    return session


def project_slug(cwd: str) -> str:
    """The directory-name encoding Claude Code uses under ~/.claude/projects."""
    return cwd.replace("/", "-")


def find_transcript(session_id: str, cwd: str) -> Path | None:
    """Locate a session's transcript, trying the slug path first.

    The fallback glob exists because a session may have started in a
    different directory than the one it currently reports.
    """
    direct = PROJECTS_DIR / project_slug(cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    try:
        for candidate in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
            return candidate
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Incremental transcript parsing.


class TranscriptCache:
    """Per-path incremental parse state over append-only transcript files.

    Transcripts are append-only, so re-reading in full on every poll would
    scale with conversation length rather than with what changed. Each file
    is resumed from a stored byte offset instead.
    """

    def __init__(self) -> None:
        self._state: dict[Path, dict] = {}

    @staticmethod
    def _blank() -> dict:
        return {
            "offset": 0,
            "title": None,
            "ai_titled": False,
            "last_prompt": None,
            "git_branch": None,
            "model": None,
            "tool_counts": {},
            "agent_uses": {},
            "completed": set(),
            "last_activity": 0.0,
            # For recovering an ended session from disk alone: the cwd it ran
            # in, whether it was a background job (sessionKind "bg") and so the
            # CLI's to report, and when it began.
            "cwd": "",
            "session_kind": None,
            "started": 0.0,
        }

    def parse(self, path: Path) -> dict:
        """Parse anything appended since the last call and return the state."""
        try:
            size = path.stat().st_size
        except OSError:
            return self._blank()

        state = self._state.get(path)
        if state is None or state["offset"] > size:
            # The file shrank: it was truncated or replaced, and our offset
            # points into bytes that no longer exist. Re-read from zero.
            state = self._blank()
            self._state[path] = state
        if state["offset"] == size:
            return state

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(state["offset"])
                for line in handle:
                    if not line.endswith("\n"):
                        # Partial trailing line: a session is mid-write. Stop
                        # before it and pick it up on the next poll, or we
                        # consume half a JSON object and lose it.
                        break
                    # Advance by BYTE length — seek() is in bytes, and len()
                    # on a str counts characters, which drift apart the first
                    # time a transcript contains anything non-ASCII.
                    state["offset"] += len(line.encode("utf-8", errors="replace"))
                    self._consume(state, line)
        except OSError:
            pass
        return state

    @staticmethod
    def _consume(state: dict, line: str) -> None:
        """Fold one JSONL entry into the state."""
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return

        entry_type = entry.get("type")
        if entry_type == "ai-title":
            title = entry.get("aiTitle")
            if title:
                state["title"] = str(title)
                state["ai_titled"] = True
        elif entry_type == "agent-name":
            # Fallback title only: an aiTitle, whenever it arrives, wins.
            name = entry.get("agentName")
            if name and not state["ai_titled"]:
                state["title"] = str(name)
        elif entry_type == "last-prompt":
            prompt = entry.get("lastPrompt")
            if prompt:
                state["last_prompt"] = str(prompt)

        branch = entry.get("gitBranch")
        if branch:
            state["git_branch"] = str(branch)

        # cwd and sessionKind ride on ordinary message entries, and are what
        # let an ended session be rebuilt from its transcript alone -- the
        # fleet list that would otherwise supply them is gone by then.
        cwd = entry.get("cwd")
        if cwd:
            state["cwd"] = str(cwd)
        kind = entry.get("sessionKind")
        if kind:
            state["session_kind"] = str(kind)

        stamp = entry.get("timestamp")
        if stamp:
            moment = _epoch_of(stamp)
            if moment > state["last_activity"]:
                state["last_activity"] = moment
            if moment and (not state["started"] or moment < state["started"]):
                state["started"] = moment

        message = entry.get("message")
        if not isinstance(message, dict):
            return
        model = message.get("model")
        if model:
            state["model"] = str(model)
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                name = str(block.get("name") or "tool")
                state["tool_counts"][name] = state["tool_counts"].get(name, 0) + 1
                if name in ("Agent", "Task"):
                    payload = block.get("input")
                    payload = payload if isinstance(payload, dict) else {}
                    state["agent_uses"][block.get("id")] = (
                        str(payload.get("description") or ""),
                        str(payload.get("subagent_type") or "agent"),
                        str(payload.get("prompt") or ""),
                    )
            elif block_type == "tool_result":
                use_id = block.get("tool_use_id")
                if use_id:
                    state["completed"].add(use_id)


def _epoch_of(stamp: object) -> float:
    """Turn an ISO-8601 timestamp (Z suffix) into an epoch float, or 0.0."""
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Subagents.


def subagents_dir(transcript: Path) -> Path:
    return transcript.parent / transcript.stem / "subagents"


def _subagent_head(path: Path) -> tuple[str, int]:
    """A subagent file's first user-message text, and its message count."""
    first_text = ""
    count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                entry_type = entry.get("type")
                if entry_type not in ("user", "assistant"):
                    continue
                count += 1
                if first_text or entry_type != "user":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    first_text = content
                elif isinstance(content, list):
                    parts = [
                        str(block.get("text") or "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    first_text = "\n".join(part for part in parts if part)
    except OSError:
        return "", 0
    return first_text, count


def load_subagents(transcript: Path, state: dict) -> list[SubAgent]:
    """Join subagent transcript files back to the parent's Agent tool calls.

    Nothing in a subagent file names it, so the join key is the prompt: a
    subagent's first user message is verbatim the `prompt` its parent passed.
    A 200-char prefix is enough to be unique and cheap to hash. An unmatched
    file is from an earlier turn, so treat it as settled and label it by its
    own opening line — silently dropping one is worse than mislabelling it.
    """
    directory = subagents_dir(transcript)
    if not directory.is_dir():
        return []

    by_prompt = {
        prompt[:200]: (use_id, description, agent_type, use_id in state["completed"])
        for use_id, (description, agent_type, prompt) in state["agent_uses"].items()
        if prompt
    }

    roster: list[SubAgent] = []
    try:
        paths = sorted(directory.glob("agent-*.jsonl"))
    except OSError:
        return []
    for path in paths:
        agent_id = path.stem[len("agent-"):] or path.stem
        first_text, count = _subagent_head(path)
        match = by_prompt.get(first_text[:200])
        if match:
            _, description, agent_type, done = match
        else:
            description = first_text[:60].strip() or agent_id
            agent_type = "agent"
            done = True
        roster.append(
            SubAgent(
                agent_id=agent_id,
                description=description,
                agent_type=agent_type,
                status="done" if done else "working",
                path=path,
                messages=count,
            )
        )
    return roster


def enrich(session: Session, cache: TranscriptCache) -> Session:
    """Fill in everything the transcript knows about a session."""
    transcript = find_transcript(session.session_id, session.cwd)
    session.transcript = transcript
    if transcript is None:
        # No transcript means no activity to measure; the start time is the
        # honest stand-in, and it keeps idle_seconds() meaningful.
        session.last_activity = session.started_at / 1000
        return session

    state = cache.parse(transcript)
    session.title = state["title"]
    session.last_prompt = state["last_prompt"]
    session.git_branch = state["git_branch"]
    session.model = state["model"]
    session.tool_counts = dict(state["tool_counts"])
    session.subagents = load_subagents(transcript, state)
    session.last_activity = state["last_activity"] or session.started_at / 1000
    return session


# ---------------------------------------------------------------------------
# Codex sessions.
#
# Codex agents live in a different world from Claude ones: there is no fleet
# command to ask, only the rollout files the CLI writes under
# ~/.codex/sessions/YYYY/MM/DD/. Everything a card needs is read from those
# files and from session_index.jsonl (the CLI's own {id, thread_name} log) --
# nothing is scraped from a screen, keeping the C3 constraint intact. Status
# is inferred from the rollout's own task_started / task_complete events,
# which is the honest ceiling: a rollout mid-task that has gone quiet was
# interrupted, and a rollout whose last task completed is a reply to read.

CODEX_ACTIVITY_WINDOW = 24 * 3600  # sessions older than this have left the board
CODEX_WORKING_FRESH = 600          # an open task silent this long was interrupted
CODEX_ROLLOUT_RE = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(.+)$"
)

_codex_titles_cache: tuple[tuple[float, int], dict[str, str]] | None = None


def codex_titles() -> dict[str, str]:
    """The CLI's own thread names, keyed by session id.

    session_index.jsonl is append-only and can grow long, so the parse is
    cached against (mtime, size) and only redone when the file changes.
    """
    global _codex_titles_cache
    try:
        stat = CODEX_INDEX_PATH.stat()
    except OSError:
        return {}
    key = (stat.st_mtime, stat.st_size)
    if _codex_titles_cache and _codex_titles_cache[0] == key:
        return _codex_titles_cache[1]
    titles: dict[str, str] = {}
    try:
        with CODEX_INDEX_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("id"):
                    titles[str(entry["id"])] = str(entry.get("thread_name") or "")
    except OSError:
        return {}
    _codex_titles_cache = (key, titles)
    return titles


class CodexCache:
    """Incremental parse state over codex rollout files.

    Same discipline as TranscriptCache -- byte offsets, stop before a partial
    trailing line, re-read on shrink -- with a consumer that understands the
    rollout vocabulary instead of the Claude Code one.
    """

    def __init__(self) -> None:
        self._state: dict[Path, dict] = {}

    @staticmethod
    def _blank() -> dict:
        return {
            "offset": 0,
            "meta_seen": False,
            "cwd": "",
            "model": None,
            "last_prompt": None,
            "tool_counts": {},
            "task_open": False,
            "last_activity": 0.0,
        }

    def parse(self, path: Path) -> dict:
        try:
            size = path.stat().st_size
        except OSError:
            return self._blank()
        state = self._state.get(path)
        if state is None or state["offset"] > size:
            state = self._blank()
            self._state[path] = state
        if state["offset"] == size:
            return state
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(state["offset"])
                for line in handle:
                    if not line.endswith("\n"):
                        # Partial trailing line: mid-write. Next poll gets it.
                        break
                    state["offset"] += len(line.encode("utf-8", errors="replace"))
                    self._consume(state, line)
        except OSError:
            pass
        return state

    @staticmethod
    def _consume(state: dict, line: str) -> None:
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        payload = entry.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        stamp = entry.get("timestamp")
        if isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                state["last_activity"] = max(state["last_activity"], parsed.timestamp())
            except ValueError:
                pass

        entry_type = entry.get("type")
        if entry_type == "session_meta":
            # A resumed rollout replays its ancestors' meta entries too; the
            # first one belongs to this file and is the one that describes it.
            if not state["meta_seen"]:
                state["meta_seen"] = True
                state["cwd"] = str(payload.get("cwd") or "")
        elif entry_type == "turn_context":
            model = payload.get("model")
            if model:
                state["model"] = str(model)
        elif entry_type == "event_msg":
            event = payload.get("type")
            if event == "user_message":
                message = str(payload.get("message") or "").strip()
                if message:
                    state["last_prompt"] = message[:500]
            elif event == "task_started":
                state["task_open"] = True
            elif event in ("task_complete", "turn_aborted"):
                state["task_open"] = False
        elif entry_type == "response_item":
            if payload.get("type") == "function_call":
                name = str(payload.get("name") or "tool")
                state["tool_counts"][name] = state["tool_counts"].get(name, 0) + 1


def _recent_rollouts(now: float) -> list[Path]:
    """Rollout files from the last week of date directories.

    The walk is bounded by date rather than globbing the whole tree: a year
    of history is on disk, and only files still being written matter here.
    A session started days ago still lives under its start date, so the walk
    reaches back further than the activity window it filters by.
    """
    from datetime import date, timedelta

    paths: list[Path] = []
    today = date.fromtimestamp(now)
    for offset in range(8):
        day = today - timedelta(days=offset)
        directory = CODEX_SESSIONS_DIR / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        try:
            paths.extend(directory.glob("rollout-*.jsonl"))
        except OSError:
            continue
    return paths


def collect_codex(cache: CodexCache) -> list[Session]:
    """Every codex session active inside the window, as ordinary Sessions.

    They ride the same rails as Claude sessions from here on: names, tags,
    overrides and read positions are all keyed by session id, so every
    feature works unchanged. Kind is "interactive" -- there is nothing to
    attach to -- and the engine field is what tells the front ends.
    """
    if not CODEX_SESSIONS_DIR.is_dir():
        return []
    now = time.time()
    sessions: list[Session] = []
    for path in _recent_rollouts(now):
        match = CODEX_ROLLOUT_RE.match(path.stem)
        if not match:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if now - mtime > CODEX_ACTIVITY_WINDOW:
            continue
        state = cache.parse(path)
        if state["task_open"]:
            # An open task that has gone quiet was interrupted, not working.
            status = "working" if now - mtime < CODEX_WORKING_FRESH else "stopped"
        else:
            status = "done"
        try:
            started = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S")
            started_ms = int(started.timestamp() * 1000)
        except ValueError:
            started_ms = int(mtime * 1000)
        session_id = match.group(2)
        sessions.append(
            Session(
                session_id=session_id,
                kind="interactive",
                status=status,
                cwd=state["cwd"],
                started_at=started_ms,
                engine="codex",
                title=codex_titles().get(session_id) or None,
                last_prompt=state["last_prompt"],
                model=state["model"] or "codex",
                tool_counts=dict(state["tool_counts"]),
                transcript=path,
                last_activity=mtime,
            )
        )
    return sessions


# ---------------------------------------------------------------------------
# The hook's waiting state.


def load_waiting_state() -> dict:
    return _read_json(HOOK_STATE_PATH)


def apply_waiting_state(session: Session, waiting: dict) -> Session:
    """Promote an idle interactive session to blocked, when the hook says so.

    Only idle → blocked, only for interactive sessions, and only when the
    transcript has no activity newer than the event. A session reporting
    `busy` is demonstrably running, which beats a hook event that may predate
    it; and activity after the event means the human already replied, so the
    wait is cleared even if the hook's clearing event never fired.
    """
    entry = waiting.get(session.session_id)
    if not isinstance(entry, dict):
        return session
    if session.kind != "interactive" or session.status != "idle":
        return session
    if not entry.get("waiting"):
        return session
    since = entry.get("since")
    if isinstance(since, (int, float)) and session.last_activity <= since:
        session.status = "blocked"
    return session


# ---------------------------------------------------------------------------
# Custom names.


def load_custom_names() -> dict:
    return _read_json(NAMES_PATH)


def save_custom_name(session_id: str, name: str) -> None:
    """Store a human-typed name for a session. An empty name clears it."""
    names = load_custom_names()
    cleaned = (name or "").strip()[:MAX_NAME_LENGTH]
    if cleaned:
        names[session_id] = cleaned
    else:
        names.pop(session_id, None)
    _write_json(NAMES_PATH, names)


# ---------------------------------------------------------------------------
# Manual overrides.


def load_overrides() -> dict:
    return _read_json(OVERRIDES_PATH)


def save_override(
    session_id: str, to: str | None, base: str, at: float
) -> tuple[bool, str]:
    """Record (or clear) a manual re-filing of a session.

    `base` and `at` must always come from the server's view of reality, never
    from the client — the card the user dragged may already be showing an
    override, and re-basing onto that pins the entry to a state that can
    never expire.
    """
    overrides = load_overrides()
    if not to or to == "auto":
        overrides.pop(session_id, None)
        _write_json(OVERRIDES_PATH, overrides)
        return True, "Back to automatic."
    if to not in OVERRIDABLE:
        return False, f"Cannot move a session to '{to}'."
    if base == "working":
        # Saying a process is not running does not stop it. Refuse honestly
        # rather than filing a live process under a settled column.
        return False, "This agent is actually working. Stop it first."
    overrides[session_id] = {"to": to, "base": base, "at": float(at)}
    _write_json(OVERRIDES_PATH, overrides)
    return True, f"Moved to {to}."


def apply_reply_promotion(session: Session, seen: dict, now: float) -> Session:
    """File an idle session with a reply on it under Replied instead of Idle.

    Idle is only the CLI's word for "the turn ended"; on this board a turn
    that ended is a reply, and a card would otherwise fall straight from
    Working into a hidden column with its answer unseen.

    Two rules land it in Replied. An interactive session -- one you steer from
    this board -- holds its Replied slot for as long as its transcript was
    touched inside INTERACTIVE_REPLY_WINDOW, whether or not you have read the
    answer: opening a reply is not the same as being done with the
    conversation, so reading no longer demotes it and only real silence (48h
    with no new activity) does. A background session keeps the older rule --
    you were attached to it, not driving it from here, so reading its reply
    returns the card to Idle -- either it has spoken since you last read it,
    or it spoke within the last hour.
    """
    if session.status != "idle":
        return session
    seen_at = seen.get(session.session_id, 0.0)
    spoke_since_read = session.last_activity > seen_at
    recent = now - session.last_activity < RECENT_REPLY_SECONDS
    fresh_interactive = (
        session.kind == "interactive"
        and now - session.last_activity < INTERACTIVE_REPLY_WINDOW
    )
    if fresh_interactive or (spoke_since_read and (seen_at > 0 or recent)):
        session.status = "done"
    return session


def apply_override(session: Session, overrides: dict) -> Session:
    """Apply a manual override, if reality has not moved on since it was made.

    Both expiry tests are needed. Without `base`, an override never expires.
    Without `at`, a session marked complete, then chatted with again, goes
    working → done, matches `base` a second time and silently jumps back to
    Done after work you did since.
    """
    session.real_status = session.status  # recorded for EVERY session
    entry = overrides.get(session.session_id)
    if not isinstance(entry, dict):
        return session
    if session.status != entry.get("base"):
        return session  # reality moved on
    at = entry.get("at")
    if isinstance(at, (int, float)) and session.last_activity > at:
        return session  # you talked to it since the move
    if entry.get("to") in OVERRIDABLE:
        session.status = entry["to"]
        session.overridden = True
    return session


# ---------------------------------------------------------------------------
# Read positions.


def load_read() -> dict:
    return _read_json(READ_PATH)


def mark_read(session_id: str, at: float) -> None:
    """Record how far you had read: the session's last_activity when opened.

    A position rather than a boolean, so the next thing the agent says makes
    the card unread again with nothing watching for it. A flag would need
    clearing by whatever noticed the new output, and nothing is watching.
    """
    store = load_read()
    store[session_id] = float(at)
    _write_json(READ_PATH, store)


def mark_unread(session_id: str) -> None:
    """Forget the read position rather than setting a flag.

    Unread is "have you seen this far", and the honest way to say no is to
    have no record at all.
    """
    store = load_read()
    store.pop(session_id, None)
    _write_json(READ_PATH, store)


# ---------------------------------------------------------------------------
# Dismissals -- cards the human has cleared off the board.


def load_dismissed() -> dict:
    return _read_json(DISMISSED_PATH)


def dismiss_session(session_id: str, at: float) -> None:
    """Clear a session's card off the board, from this reply onward.

    Stored as a position, not a flag, exactly like a read mark: the session
    is hidden only while its last activity is no newer than the moment you
    dismissed it. A closed or idle session never speaks again, so it stays
    gone -- which is the whole point of the button -- but a session that turns
    out to be alive and says something new earns its card back on its own,
    with nothing watching to un-hide it. Deleting is therefore safe: the worst
    it can do to a live session is hide it until its next line.
    """
    store = load_dismissed()
    store[session_id] = float(at)
    _write_json(DISMISSED_PATH, store)


def restore_session(session_id: str) -> None:
    """Undo a dismissal by forgetting the position."""
    store = load_dismissed()
    store.pop(session_id, None)
    _write_json(DISMISSED_PATH, store)


def is_dismissed(session: Session, dismissed: dict) -> bool:
    """True while a dismissed session has said nothing new since it was cleared."""
    at = dismissed.get(session.session_id)
    return at is not None and session.last_activity <= at


# ---------------------------------------------------------------------------
# Tags.


def load_tags() -> dict:
    return _read_json(TAGS_PATH)


def normalize_tags(tags: object) -> list[str]:
    """Trim, cap and de-dupe a tag list.

    De-duplication is case-insensitive keeping the first spelling: "PR review"
    and "PR Review" are the same label to a human, and a filter that treats
    them as two entries is worse than useless.
    """
    if not isinstance(tags, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        text = " ".join(tag.split())[:MAX_TAG_LENGTH].strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= MAX_TAGS_PER_SESSION:
            break
    return cleaned


def save_tags(session_id: str, tags: object) -> list[str]:
    """Store a session's tags, normalised. An empty list removes the entry."""
    store = load_tags()
    cleaned = normalize_tags(tags)
    if cleaned:
        store[session_id] = cleaned
    else:
        store.pop(session_id, None)
    _write_json(TAGS_PATH, store)
    return cleaned


# ---------------------------------------------------------------------------
# Ended interactive sessions, read back from disk.


def recover_ended_interactive(
    cache: TranscriptCache, live_ids: set[str], now: float
) -> list[Session]:
    """Interactive sessions whose process has ended, rebuilt from their transcript.

    `claude agents` lists a session only while its process is alive, so an
    interactive (terminal) session drops off the fleet the instant its tab
    closes -- and with no disk fallback its card simply vanished, unread reply
    and all. The transcript is still on disk, so a recently-touched one the
    fleet no longer knows about is resurfaced here as an ordinary Session and
    filed by the same Idle/Replied rules as everything else. This is the same
    second source Codex has always leaned on, extended to Claude.

    Deliberately narrow, to bring back exactly the vanished cards and nothing
    more. Background transcripts (`sessionKind: "bg"`) are skipped because the
    CLI keeps reporting finished background jobs itself; a session still in the
    live fleet is always the fleet's, never a stale disk copy; and only a
    transcript with real activity inside the window earns a card, so an empty
    shell is never resurrected.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    try:
        paths = list(PROJECTS_DIR.glob("*/*.jsonl"))
    except OSError:
        return []

    recovered: list[Session] = []
    for path in paths:
        session_id = path.stem
        if session_id in live_ids:
            continue  # still running -- the fleet's copy is the source of truth
        try:
            if now - path.stat().st_mtime > ENDED_INTERACTIVE_WINDOW:
                continue
        except OSError:
            continue
        state = cache.parse(path)
        if state["session_kind"] == "bg":
            continue  # a background job -- the CLI still lists these itself
        if not state["last_activity"]:
            continue  # a shell with no real turn; nothing worth a card
        started = state["started"] or state["last_activity"]
        session = Session(
            session_id=session_id,
            kind="interactive",
            status="idle",  # ended -> idle, then promoted to Replied in collect()
            cwd=state["cwd"],
            started_at=int(started * 1000),
        )
        session.transcript = path
        session.title = state["title"]
        session.last_prompt = state["last_prompt"]
        session.git_branch = state["git_branch"]
        session.model = state["model"]
        session.tool_counts = dict(state["tool_counts"])
        session.subagents = load_subagents(path, state)
        session.last_activity = state["last_activity"]
        recovered.append(session)
    return recovered


# ---------------------------------------------------------------------------
# The full refresh.


def collect(cache: TranscriptCache | None = None) -> tuple[list[Session], str | None]:
    """One full refresh of the fleet. Runs on a poll; must not write files.

    The order of operations is load-bearing. The hook can promote
    idle → blocked, but a manual move is your judgement about the RESULT, so
    it is applied last and wins. And `unread` is computed AFTER enrich, never
    before: `last_activity` comes out of the transcript, so until then it is
    0.0 and every session compares as already read.
    """
    sessions, error = list_sessions()
    if error:
        return [], error
    if cache is None:
        cache = TranscriptCache()

    waiting = load_waiting_state()
    names = load_custom_names()
    overrides = load_overrides()
    tags = load_tags()
    seen = load_read()

    enriched: list[Session] = []
    for session in sessions:
        session.custom_name = names.get(session.session_id)
        session.tags = normalize_tags(tags.get(session.session_id))
        # Tempo is a fact about reality, so it is corrected before the hook's
        # promotion and before overrides -- an override's expiry must compare
        # against what the session is really doing, not the CLI's stuck field.
        ready = apply_waiting_state(apply_tempo(enrich(session, cache)), waiting)
        enriched.append(ready)

    now = time.time()

    # An interactive session vanishes from the fleet the moment its terminal
    # closes; read the recently-ended ones back from disk so a closed tab no
    # longer deletes its card. They arrive already enriched, so they only need
    # the human-judgement files here, then ride the same Idle/Replied + override
    # + unread pass below as any fleet session.
    live_ids = {session.session_id for session in sessions}
    for recovered in recover_ended_interactive(cache, live_ids, now):
        recovered.custom_name = names.get(recovered.session_id)
        recovered.tags = normalize_tags(tags.get(recovered.session_id))
        enriched.append(recovered)

    for index, ready in enumerate(enriched):
        ready = apply_reply_promotion(ready, seen, now)
        ready = apply_override(ready, overrides)
        ready.unread = ready.last_activity > seen.get(ready.session_id, 0.0)
        enriched[index] = ready

    # Codex sessions arrive already enriched -- their collector read the
    # rollout itself -- so they skip enrich/tempo/hook and join at the
    # names/tags/overrides/read stage, which is keyed by session id and
    # engine-agnostic. The codex cache hangs off the shared cache object so
    # both front ends keep their one-cache-per-poller shape.
    codex_cache = getattr(cache, "_codex", None)
    if codex_cache is None:
        codex_cache = CodexCache()
        setattr(cache, "_codex", codex_cache)
    for session in collect_codex(codex_cache):
        session.custom_name = names.get(session.session_id)
        session.tags = normalize_tags(tags.get(session.session_id))
        ready = apply_override(session, overrides)
        ready.unread = ready.last_activity > seen.get(session.session_id, 0.0)
        enriched.append(ready)

    # Cards the human cleared off the board drop out last, after every source
    # has been folded in, so one pass covers fleet, recovered and codex alike.
    # A dismissal that has been outlived by new activity is silently ignored --
    # the session speaks again and its card returns -- so this poll-time read
    # never has to prune the file, keeping collect() write-free.
    dismissed = load_dismissed()
    enriched = [s for s in enriched if not is_dismissed(s, dismissed)]

    enriched.sort(key=lambda s: s.sort_key)
    return enriched, None
