"""The curses front end: a grid of cards over every session on the machine.

The design decision worth keeping: the terminal grid is drawn entirely through
one clipping-safe `put` helper against whatever window it is handed, which is
what lets the layout tests run the real drawing code against a fake screen and
assert on the resulting character grid. Nothing in this module talks to
`http.server`, `notes` or the web front end -- it imports `discovery`,
`terminal` and `transcript` only, so stubbing `curses` under test drags in
nothing heavier than the data model.

Colour is deliberately lopsided. A saturated hue means "this session is alive
and its state matters"; everything structural -- frames, rules, labels,
secondary text -- is a neutral grey ramp. An earlier version mapped the dim
pair to COLOR_BLUE, which put a saturated hue on roughly every border and
subtitle on screen and left nothing for status to stand out against.
"""

from __future__ import annotations

import curses
import locale
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import discovery, terminal, transcript

# ---------------------------------------------------------------------------
# Views

VIEW_GRID = "grid"
VIEW_DETAIL = "detail"
VIEW_TRANSCRIPT = "transcript"

FILTERS = ("all", "active", "needs you", "recent")

# ---------------------------------------------------------------------------
# Colour pair ids

C_BLOCKED = 1
C_WORKING = 2
C_DONE = 3
C_FAILED = 4
C_IDLE = 5
C_SELECTED = 6
C_HEADER = 7
C_DIM = 8
C_ACCENT = 9
C_TITLE = 10
C_MUTED = 11
C_FRAME = 12
C_CHIP_BLOCKED = 13
C_CHIP_FAILED = 14
C_CHIP_WORKING = 15
C_EDGE_BLOCKED = 16
C_EDGE_WORKING = 17

# xterm-256 indices, chosen so the three greys are visibly separated on a dark
# background and the status hues share a similar perceived lightness -- no card
# looks louder purely because of its hue.
PALETTE_256 = {
    C_BLOCKED: 214,   # amber -- needs a human
    C_WORKING: 45,    # cyan  -- running
    C_DONE: 71,       # muted green, deliberately not the brightest green
    C_FAILED: 203,    # soft red
    C_IDLE: 244,      # grey: idle carries no hue at all, so it recedes
    C_SELECTED: 231,  # near-white: the cursor must be findable on any card
    C_HEADER: 141,    # violet, used ONLY for the wordmark
    C_DIM: 240,
    C_ACCENT: 214,
    C_TITLE: 253,
    C_MUTED: 245,
    C_FRAME: 237,
    C_EDGE_BLOCKED: 136,
    C_EDGE_WORKING: 31,
}

# ---------------------------------------------------------------------------
# Geometry

CARD_CHROME = 6          # borders + title + subtitle + prompt + tools
MAX_SUBAGENT_ROWS = 3
MIN_CARD_WIDTH = 44
MAX_CARD_WIDTH = 62
REFRESH_SECONDS = 2.0
RECENT_SECONDS = 24 * 3600
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
GRID_TOP = 2

# Selection is heavy box-drawing rules, not reverse video -- a reversed colour
# pair across a whole border reads as a solid block and fights the status
# colours.
BORDER_LIGHT = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
BORDER_HEAVY = {"tl": "┏", "tr": "┓", "bl": "┗", "br": "┛", "h": "━", "v": "┃"}

STATUS_LABELS = {
    "blocked": "NEEDS YOU",
    "working": "WORKING",
    "failed": "FAILED",
    "done": "REPLIED",
    "stopped": "STOPPED",
    "idle": "IDLE",
    "complete": "DONE",
    "unknown": "?",
}

# Chips invert and are reserved for states a human must act on, so the chip
# stays rare enough to keep its urgency. Frame tints exist only for blocked /
# working / failed; everything else keeps the neutral frame, which is what
# makes a screen full of idle sessions calm instead of noisy.
CHIP_PAIRS = {"blocked": C_CHIP_BLOCKED, "failed": C_CHIP_FAILED}
EDGE_TINTS = {"blocked": C_EDGE_BLOCKED, "working": C_EDGE_WORKING, "failed": C_FAILED}
HUE_PAIRS = {
    "working": C_WORKING,
    "done": C_DONE,
    "stopped": C_DIM,
    "idle": C_IDLE,
    "complete": C_DONE,
    "unknown": C_DIM,
}


# ---------------------------------------------------------------------------
# Small helpers


def _clip(text: str, width: int) -> str:
    """Truncate to a width with a single-character ellipsis."""
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def format_age(seconds: float) -> str:
    """Render an age the way a human compares them: 45s, 2m, 3h, 2d."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _is_within(candidate: str, root: str) -> bool:
    """Whether `candidate` is `root` or lives under it, by path component.

    Resolve both sides -- comparing an unresolved path against a resolved one
    silently fails on macOS, where /var is a symlink to /private/var. Compare
    by path component, so ~/repo does not match the sibling ~/repo-tests the
    way a string prefix check would.
    """
    try:
        resolved = Path(candidate).expanduser().resolve()
        base = Path(root).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return resolved == base or base in resolved.parents


def _key_in(key: int, *names: str) -> bool:
    """Compare against named curses keys only when they are real integers.

    Under test `curses` is a MagicMock and its KEY_* attributes are mocks;
    comparing against those must read as "no match", never raise.
    """
    for name in names:
        value = getattr(curses, name, None)
        if isinstance(value, int) and key == value:
            return True
    return False


def init_colours() -> None:
    """Register every pair, with an 8-colour fallback that keeps idle plain."""
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return
    # Probe defensively -- curses is a MagicMock in the tests, where any
    # comparison against COLORS raises rather than answering.
    try:
        rich = int(curses.COLORS) >= 256
    except (TypeError, ValueError, AttributeError):
        rich = False
    try:
        if rich:
            for pair, colour_index in PALETTE_256.items():
                curses.init_pair(pair, colour_index, -1)
            curses.init_pair(C_CHIP_BLOCKED, 16, PALETTE_256[C_BLOCKED])
            curses.init_pair(C_CHIP_FAILED, 16, PALETTE_256[C_FAILED])
            curses.init_pair(C_CHIP_WORKING, 16, PALETTE_256[C_WORKING])
        else:
            fallback = {
                C_BLOCKED: curses.COLOR_YELLOW,
                C_WORKING: curses.COLOR_CYAN,
                C_DONE: curses.COLOR_GREEN,
                C_FAILED: curses.COLOR_RED,
                C_HEADER: curses.COLOR_MAGENTA,
                C_ACCENT: curses.COLOR_YELLOW,
                C_SELECTED: curses.COLOR_WHITE,
                C_EDGE_BLOCKED: curses.COLOR_YELLOW,
                C_EDGE_WORKING: curses.COLOR_CYAN,
                # Idle and the secondary text stay the default foreground so
                # idle still reads as unremarkable; the frame recedes to black.
                C_IDLE: -1,
                C_TITLE: -1,
                C_MUTED: -1,
                C_FRAME: curses.COLOR_BLACK,
                C_DIM: curses.COLOR_BLACK,
            }
            for pair, colour_index in fallback.items():
                curses.init_pair(pair, colour_index, -1)
            curses.init_pair(C_CHIP_BLOCKED, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            curses.init_pair(C_CHIP_FAILED, curses.COLOR_BLACK, curses.COLOR_RED)
            curses.init_pair(C_CHIP_WORKING, curses.COLOR_BLACK, curses.COLOR_CYAN)
    except curses.error:
        pass


# ---------------------------------------------------------------------------
# Background refresh


class Poller:
    """Worker-thread refresh so a slow `claude agents` never stutters input.

    A full poll costs roughly half a second, which would visibly stutter
    keyboard handling if it ran on the UI loop. `request()` is a no-op while a
    poll is already in flight, so holding `r` down cannot stack threads.
    """

    def __init__(self, cache: discovery.TranscriptCache | None = None) -> None:
        self._cache = cache if cache is not None else discovery.TranscriptCache()
        self._lock = threading.Lock()
        self._busy = False
        self._generation = 0
        self._result: tuple[list, str | None] | None = None

    def request(self) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self) -> None:
        sessions, error = discovery.collect(self._cache)
        with self._lock:
            self._result = (sessions, error)
            self._generation += 1
            self._busy = False

    def take(self) -> tuple[list, str | None] | None:
        """Hand over the latest completed poll, at most once."""
        with self._lock:
            result, self._result = self._result, None
            return result


# ---------------------------------------------------------------------------
# The grid


class AgentGrid:
    """One class holding the view state, the modals, and per-frame hitboxes."""

    def __init__(self, stdscr, options=None) -> None:
        self.stdscr = stdscr
        # Read options with getattr defaults so a bare object (or None) works.
        filter_name = str(getattr(options, "filter", "all") or "all")
        filter_name = filter_name.replace("-", " ").replace("_", " ")
        self.filter_name = filter_name if filter_name in FILTERS else "all"
        self.cwd = getattr(options, "cwd", None)
        self.no_mouse = bool(getattr(options, "no_mouse", False))
        try:
            self.refresh_seconds = float(getattr(options, "refresh", REFRESH_SECONDS) or REFRESH_SECONDS)
        except (TypeError, ValueError):
            self.refresh_seconds = REFRESH_SECONDS

        self.sessions: list = []
        self.error: str | None = None
        self.loaded = False

        self.view = VIEW_GRID
        self.selected = 0
        self.detail_row = 0
        self.transcript_scroll = 0
        self.transcript_lines: list[tuple[str, int]] = []
        self.transcript_path: Path | None = None

        # Hitboxes are rebuilt every frame -- computed once they go stale as
        # soon as the layout changes, and clicks stop mapping to what is drawn.
        self.hitboxes: list[tuple[int, int, int, int, int]] = []
        self.detail_hitboxes: list[tuple[int, int]] = []
        self._columns = 1

        self.pending_prompt: dict | None = None
        self.pending_confirm: dict | None = None
        self.status_message = ""
        self.message_until = 0.0

        self.cache = discovery.TranscriptCache()
        self.poller = Poller(self.cache)

    # -- drawing primitives -------------------------------------------------

    def put(self, y, x, text, attr=0):
        """Clipping-safe write. Without it, curses raises at the bottom-right
        corner and on any overflow."""
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        text = text[: max(0, width - x)]
        if y == height - 1 and x + len(text) >= width:
            text = text[: width - x - 1]
        if not text:
            return
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def colour(self, pair: int, bold: bool = False):
        try:
            attr = curses.color_pair(pair)
        except Exception:
            attr = 0
        if bold:
            try:
                attr = attr | curses.A_BOLD
            except Exception:
                pass
        return attr

    # -- small state helpers ------------------------------------------------

    def flash(self, message: str) -> None:
        self.status_message = message
        self.message_until = time.time() + 3.0

    def ask(self, message: str, action) -> None:
        """Queue a confirmation. Only y/Y runs the action; anything cancels."""
        self.pending_confirm = {"message": message, "action": action}

    def start_prompt(self, label: str, initial: str, callback) -> None:
        self.pending_prompt = {"label": label, "text": initial, "callback": callback}

    # -- session selection --------------------------------------------------

    def matches_filter(self, session) -> bool:
        if self.cwd and not _is_within(session.cwd, self.cwd):
            return False
        if self.filter_name == "active":
            return session.status in ("blocked", "working", "failed", "done")
        if self.filter_name == "needs you":
            return session.status in ("blocked", "failed")
        if self.filter_name == "recent":
            return session.idle_seconds() < RECENT_SECONDS
        return True

    def visible_sessions(self) -> list:
        return [s for s in self.sessions if self.matches_filter(s)]

    def selected_session(self):
        sessions = self.visible_sessions()
        if not sessions:
            return None
        self.selected = max(0, min(self.selected, len(sessions) - 1))
        return sessions[self.selected]

    def _cycle_filter(self) -> None:
        index = FILTERS.index(self.filter_name) if self.filter_name in FILTERS else 0
        self.filter_name = FILTERS[(index + 1) % len(FILTERS)]
        self.selected = 0
        self.flash(f"filter: {self.filter_name}")

    def _jump_to_blocked(self) -> None:
        """Tab walks the sessions waiting on a human, wrapping."""
        sessions = self.visible_sessions()
        if not sessions:
            return
        count = len(sessions)
        for step in range(1, count + 1):
            index = (self.selected + step) % count
            if sessions[index].status in ("blocked", "failed"):
                self.selected = index
                return
        self.flash("Nothing is waiting on you.")

    def sync(self) -> None:
        result = self.poller.take()
        if result is None:
            return
        sessions, error = result
        self.loaded = True
        if error:
            # Keep the last good snapshot; only the error line changes.
            self.error = error
            return
        self.error = None
        self.sessions = sessions
        if self.visible_sessions():
            self.selected = max(0, min(self.selected, len(self.visible_sessions()) - 1))
        else:
            self.selected = 0

    # -- drawing ------------------------------------------------------------

    def draw(self) -> None:
        self.stdscr.erase()
        if self.view == VIEW_GRID:
            self.draw_grid()
        elif self.view == VIEW_DETAIL:
            self.draw_detail()
        else:
            self.draw_transcript()
        self.draw_footer()
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_header(self, subtitle: str) -> None:
        self.put(0, 1, "agentgrid", self.colour(C_HEADER, bold=True))
        self.put(0, 11, subtitle, self.colour(C_DIM))

    def draw_grid(self) -> None:
        height, width = self.stdscr.getmaxyx()
        sessions = self.visible_sessions()
        self.hitboxes = []

        blocked = sum(1 for s in sessions if s.status in ("blocked", "failed"))
        summary = f"{len(sessions)} sessions"
        if blocked:
            summary += f" · {blocked} need you"
        if self.filter_name != "all":
            summary += f" · filter: {self.filter_name}"
        if self.cwd:
            summary += f" · under {self.cwd}"
        self._draw_header(summary)

        if self.error:
            self.put(1, 1, _clip(self.error, width - 2), self.colour(C_FAILED))
        if not sessions:
            message = "no sessions" if self.loaded else "loading…"
            if self.filter_name != "all" and self.loaded:
                message += f" match '{self.filter_name}' (f cycles the filter)"
            self.put(GRID_TOP + 1, 2, message, self.colour(C_MUTED))
            return

        usable = max(MIN_CARD_WIDTH, width - 2)
        columns = max(1, usable // MIN_CARD_WIDTH)
        card_width = min(MAX_CARD_WIDTH, usable // columns)
        # Every card is the same height so the grid stays aligned; the height
        # is driven by the busiest session in the FILTERED set, capped at
        # three subagent rows.
        max_subagents = 0
        for session in sessions:
            max_subagents = max(max_subagents, len(session.subagents))
        card_height = CARD_CHROME + min(MAX_SUBAGENT_ROWS, max_subagents)
        self._columns = columns

        rows_available = max(1, (height - GRID_TOP - 2) // card_height)
        per_page = max(1, columns * rows_available)
        self.selected = max(0, min(self.selected, len(sessions) - 1))
        pages = (len(sessions) + per_page - 1) // per_page
        page = self.selected // per_page
        start = page * per_page

        for index in range(start, min(start + per_page, len(sessions))):
            slot = index - start
            row, column = divmod(slot, columns)
            y = GRID_TOP + row * card_height
            x = 1 + column * card_width
            self.draw_card(y, x, card_width, card_height, sessions[index], index == self.selected)
            self.hitboxes.append((y, x, y + card_height - 1, x + card_width - 1, index))

        if pages > 1:
            self.put(height - 2, 1, f"page {page + 1}/{pages}", self.colour(C_DIM))

    def _status_chip(self, session) -> tuple[str, int]:
        label = STATUS_LABELS.get(session.status, str(session.status).upper())
        chip = CHIP_PAIRS.get(session.status)
        if chip is not None:
            # Padded with spaces so the filled chip reads as a solid block
            # rather than inverted text jammed against the frame.
            return f"  {label}  ", self.colour(chip, bold=True)
        return label, self.colour(HUE_PAIRS.get(session.status, C_DIM))

    def _frame_colour(self, session, selected: bool):
        # Selected wins over status -- the cursor has to be findable even on a
        # card whose own state is unremarkable.
        if selected:
            return self.colour(C_SELECTED, bold=True)
        tint = EDGE_TINTS.get(session.status)
        if tint is not None:
            return self.colour(tint)
        return self.colour(C_FRAME)

    def draw_card(self, y, x, width, height, session, selected) -> None:
        border = BORDER_HEAVY if selected else BORDER_LIGHT
        frame = self._frame_colour(session, selected)
        inner = max(0, width - 2)

        # Top rule: project + kind marker left, status chip hard right. The
        # kind lives here, not on the branch row -- appending it there
        # truncated both fields into unreadability.
        self.put(y, x, border["tl"] + border["h"] * inner + border["tr"], frame)
        chip_text, chip_attr = self._status_chip(session)
        label = f" {session.project}"
        if getattr(session, "engine", "claude") == "codex":
            label += " · codex"   # a different CLI entirely earns its marker
        elif session.kind == "background":
            label += " · bg"   # interactive is the unmarked default
        label += " "
        label_room = max(0, inner - len(chip_text) - 3)
        self.put(y, x + 2, _clip(label, label_room), self.colour(C_MUTED))
        self.put(y, x + width - 1 - len(chip_text) - 1, chip_text, chip_attr)

        for row in range(1, height - 1):
            self.put(y + row, x, border["v"], frame)
            self.put(y + row, x + width - 1, border["v"], frame)

        content_x = x + 2
        content_width = max(0, width - 4)

        # Row 1: title left, age right-aligned -- the one number cards are
        # compared by belongs in a fixed, scannable place.
        title = session.display_title
        if session.status == "working":
            title = SPINNER[int(time.time() * 6) % len(SPINNER)] + " " + title
        age = format_age(session.age_seconds()) + " ago"
        title_room = max(0, content_width - len(age) - 1)
        # Titles tier by liveness: live work draws bright and bold, settled
        # work one grey down -- a card that finished long ago must not compete
        # with one running now.
        live = session.status in ("blocked", "working", "failed")
        title_attr = self.colour(C_TITLE, bold=True) if live else self.colour(C_MUTED)
        self.put(y + 1, content_x, _clip(title, title_room), title_attr)
        self.put(y + 1, content_x + content_width - len(age), age, self.colour(C_DIM))

        # Row 2: branch ONLY.
        self.put(y + 2, content_x, _clip(session.git_branch or "", content_width),
                 self.colour(C_MUTED))

        # Row 3: last prompt behind a single guillemet -- quotes cost two
        # glyphs per card and the marker already says this is quoted.
        prompt = " ".join(str(session.last_prompt or "").split())
        if prompt:
            self.put(y + 3, content_x, _clip("› " + prompt, content_width),
                     self.colour(C_DIM))

        # Row 4: subagent count + top three tools.
        parts = []
        if session.subagents:
            running = sum(1 for agent in session.subagents if agent.status == "working")
            part = f"{len(session.subagents)} subagents"
            if running:
                part += f" ({running} running)"
            parts.append(part)
        top_tools = sorted(session.tool_counts.items(), key=lambda item: -item[1])[:3]
        parts.extend(f"{name} {count}" for name, count in top_tools)
        if parts:
            self.put(y + 4, content_x, _clip(" · ".join(parts), content_width),
                     self.colour(C_DIM))

        # Subagent rows: 0-3, overflow collapsing the last visible row.
        sub_rows = height - CARD_CHROME
        if sub_rows > 0 and session.subagents:
            agents = session.subagents
            for index in range(min(sub_rows, len(agents))):
                row_y = y + 5 + index
                if index == sub_rows - 1 and len(agents) > sub_rows:
                    hidden = len(agents) - (sub_rows - 1)
                    self.put(row_y, content_x,
                             _clip(f"+{hidden} more subagents", content_width),
                             self.colour(C_DIM))
                    break
                agent = agents[index]
                running = agent.status == "working"
                marker = "> " if running else ". "
                messages = f"{agent.messages}msg"
                left_room = max(0, content_width - len(messages) - 1)
                self.put(row_y, content_x,
                         _clip(f"{marker}{agent.agent_type}  {agent.description}", left_room),
                         self.colour(C_WORKING if running else C_DIM))
                self.put(row_y, content_x + content_width - len(messages), messages,
                         self.colour(C_DIM))

        self.put(y + height - 1, x, border["bl"] + border["h"] * inner + border["br"], frame)

    def draw_detail(self) -> None:
        session = self.selected_session()
        if session is None:
            self.view = VIEW_GRID
            self.draw_grid()
            return
        height, width = self.stdscr.getmaxyx()
        self._draw_header("session detail · Esc back")
        self.detail_hitboxes = []

        y = 2
        title = session.display_title
        if session.overridden:
            title += "  · moved"
        self.put(y, 2, _clip(title, width - 4), self.colour(C_TITLE, bold=True))
        chip_text, chip_attr = self._status_chip(session)
        self.put(y, max(2, width - 2 - len(chip_text)), chip_text, chip_attr)
        y += 1

        status = session.status
        if session.overridden and session.real_status:
            status += f" (really {session.real_status})"
        meta = [
            ("where", f"{session.project} · {session.kind}"),
            ("status", status),
            ("cwd", session.cwd),
            ("branch", session.git_branch or "—"),
            ("model", session.model or "—"),
            ("session", session.session_id),
            ("job", session.job_id or "— (interactive)"),
            ("started", format_age(session.age_seconds()) + " ago"),
            ("activity", format_age(session.idle_seconds()) + " ago"),
        ]
        if session.tags:
            meta.append(("tags", ", ".join(session.tags)))
        for label, value in meta:
            self.put(y, 2, f"{label:>8}", self.colour(C_DIM))
            self.put(y, 12, _clip(str(value), width - 14), self.colour(C_MUTED))
            y += 1
        prompt = " ".join(str(session.last_prompt or "").split())
        if prompt:
            self.put(y, 2, _clip("› " + prompt, width - 4), self.colour(C_DIM))
            y += 1
        if session.tool_counts:
            tools = " · ".join(f"{name} {count}" for name, count
                               in sorted(session.tool_counts.items(), key=lambda item: -item[1]))
            self.put(y, 2, _clip("tools: " + tools, width - 4), self.colour(C_DIM))
            y += 1

        y += 1
        self.put(y, 2, "conversations", self.colour(C_TITLE, bold=True))
        self.put(y, 16, "(Enter opens the transcript)", self.colour(C_DIM))
        y += 1

        rows: list[str] = [f"conversation — {session.display_title}"]
        for agent in session.subagents:
            marker = ">" if agent.status == "working" else "."
            rows.append(f"{marker} {agent.agent_type}  {agent.description}")
        self.detail_row = max(0, min(self.detail_row, len(rows) - 1))
        for index, text in enumerate(rows):
            if y >= height - 2:
                # The roster is capped by the screen; the footer stays visible.
                self.put(y - 1, 2, _clip(f"… {len(rows) - index} more", width - 4),
                         self.colour(C_DIM))
                break
            chosen = index == self.detail_row
            prefix = "› " if chosen else "  "
            attr = self.colour(C_SELECTED, bold=True) if chosen else self.colour(C_MUTED)
            right = ""
            if index > 0:
                right = f"{session.subagents[index - 1].messages}msg"
            self.put(y, 2, _clip(prefix + text, width - 4 - (len(right) + 1 if right else 0)), attr)
            if right:
                self.put(y, width - 2 - len(right), right, self.colour(C_DIM))
            self.detail_hitboxes.append((y, index))
            y += 1

    def draw_transcript(self) -> None:
        session = self.selected_session()
        height, width = self.stdscr.getmaxyx()
        if session is None:
            self.view = VIEW_GRID
            self.draw_grid()
            return
        which = "conversation"
        if self.detail_row > 0 and self.detail_row - 1 < len(session.subagents):
            agent = session.subagents[self.detail_row - 1]
            which = f"{agent.agent_type} — {agent.description}"
        self._draw_header(_clip(f"{session.display_title} · {which} · Esc back", width - 12))

        body_top = 2
        body_height = max(1, height - body_top - 2)
        lines = self.transcript_lines
        max_scroll = max(0, len(lines) - body_height)
        self.transcript_scroll = max(0, min(self.transcript_scroll, max_scroll))
        window = lines[self.transcript_scroll: self.transcript_scroll + body_height]
        for offset, (text, pair) in enumerate(window):
            self.put(body_top + offset, 1, text, self.colour(pair) if pair else 0)
        if lines:
            shown_to = min(len(lines), self.transcript_scroll + body_height)
            self.put(height - 2, 1, f"{shown_to}/{len(lines)} lines", self.colour(C_DIM))
        else:
            self.put(body_top, 2, "(empty transcript)", self.colour(C_MUTED))

    def _hints(self) -> list[tuple[str, str]]:
        if self.view == VIEW_GRID:
            return [("↵", "open"), ("a", "attach"), ("x", "stop"), ("⇧R", "rename"),
                    ("Tab", "next blocked"), ("f", self.filter_name), ("r", "refresh"),
                    ("q", "quit")]
        if self.view == VIEW_DETAIL:
            return [("↵", "transcript"), ("↑↓", "agents"), ("a", "attach"), ("x", "stop"),
                    ("⇧R", "rename"), ("Esc", "back"), ("q", "quit")]
        return [("↑↓", "scroll"), ("u/d", "page"), ("g/G", "top/bottom"), ("r", "refresh"),
                ("Esc", "back"), ("q", "quit")]

    def draw_footer(self) -> None:
        height, width = self.stdscr.getmaxyx()
        y = height - 1
        if self.pending_prompt is not None:
            prompt = self.pending_prompt
            text = f"{prompt['label']}: {prompt['text']}▏"
            self.put(y, 1, _clip(text, width - 26), self.colour(C_TITLE, bold=True))
            self.put(y, min(width - 2, len(text) + 3), "Enter save · Esc cancel",
                     self.colour(C_DIM))
            return
        if self.pending_confirm is not None:
            self.put(y, 1, _clip(self.pending_confirm["message"] + "  [y/N]", width - 2),
                     self.colour(C_BLOCKED, bold=True))
            return
        if self.status_message and time.time() < self.message_until:
            self.put(y, 1, _clip(self.status_message, width - 2), self.colour(C_TITLE))
            return
        if self.error:
            self.put(y, 1, _clip(self.error, width - 2), self.colour(C_FAILED))
            return
        # Two-tone: key bright bold, label dim. A single-colour row of bare
        # lowercase keys once rendered "R rename" as "press r" -- which is
        # bound to refresh, and refresh was not listed at all.
        x = 1
        for key_text, label in self._hints():
            if x + len(key_text) + len(label) + 3 >= width:
                break
            self.put(y, x, key_text, self.colour(C_TITLE, bold=True))
            x += len(key_text) + 1
            self.put(y, x, label, self.colour(C_DIM))
            x += len(label) + 2

    # -- input --------------------------------------------------------------

    def handle_key(self, key) -> bool:
        """Dispatch one key. Returns False when the app should quit."""
        # A pending prompt swallows EVERY key, including q, so names can
        # contain it; a pending confirmation swallows every key until
        # answered.
        if self.pending_prompt is not None:
            return self._prompt_key(key)
        if self.pending_confirm is not None:
            return self._confirm_key(key)
        if self.view == VIEW_GRID:
            return self._grid_key(key)
        if self.view == VIEW_DETAIL:
            return self._detail_key(key)
        return self._transcript_key(key)

    def _prompt_key(self, key) -> bool:
        prompt = self.pending_prompt
        if key in (10, 13) or _key_in(key, "KEY_ENTER"):
            self.pending_prompt = None
            prompt["callback"](prompt["text"])
        elif key == 27:
            self.pending_prompt = None
        elif key in (127, 8) or _key_in(key, "KEY_BACKSPACE"):
            prompt["text"] = prompt["text"][:-1]
        elif key == 21:   # Ctrl-U clears
            prompt["text"] = ""
        elif isinstance(key, int) and 32 <= key < 127 and len(prompt["text"]) < 60:
            prompt["text"] += chr(key)
        return True

    def _confirm_key(self, key) -> bool:
        pending = self.pending_confirm
        self.pending_confirm = None
        if key in (ord("y"), ord("Y")):
            pending["action"]()
        else:
            self.flash("Cancelled.")
        return True

    def _grid_key(self, key) -> bool:
        if key == ord("q"):
            return False
        if key == ord("r"):
            self.poller.request()
            self.flash("Refreshing…")
            return True
        if key == ord("R"):
            self.rename_selected()
            return True
        if key == ord("f"):
            self._cycle_filter()
            return True
        if key == ord("a"):
            self.attach_selected()
            return True
        if key == ord("x"):
            self.stop_selected()
            return True
        if key == 9:   # Tab
            self._jump_to_blocked()
            return True
        if key in (10, 13) or _key_in(key, "KEY_ENTER"):
            self.open_detail()
            return True
        sessions = self.visible_sessions()
        if not sessions:
            return True
        columns = max(1, self._columns)
        if key == ord("h") or _key_in(key, "KEY_LEFT"):
            self.selected -= 1
        elif key == ord("l") or _key_in(key, "KEY_RIGHT"):
            self.selected += 1
        elif key == ord("k") or _key_in(key, "KEY_UP"):
            self.selected -= columns
        elif key == ord("j") or _key_in(key, "KEY_DOWN"):
            self.selected += columns
        self.selected = max(0, min(self.selected, len(sessions) - 1))
        return True

    def _detail_key(self, key) -> bool:
        if key == ord("q"):
            return False
        if key == 27:
            self.view = VIEW_GRID
            return True
        if key in (10, 13) or _key_in(key, "KEY_ENTER"):
            self.open_transcript()
            return True
        if key == ord("R"):
            self.rename_selected()
            return True
        if key == ord("a"):
            self.attach_selected()
            return True
        if key == ord("x"):
            self.stop_selected()
            return True
        if key == ord("r"):
            self.poller.request()
            return True
        session = self.selected_session()
        rows = 1 + (len(session.subagents) if session else 0)
        if key == ord("k") or _key_in(key, "KEY_UP"):
            self.detail_row -= 1
        elif key == ord("j") or _key_in(key, "KEY_DOWN"):
            self.detail_row += 1
        self.detail_row = max(0, min(self.detail_row, rows - 1))
        return True

    def _transcript_key(self, key) -> bool:
        if key == ord("q"):
            return False
        if key == 27:
            self.view = VIEW_DETAIL
            return True
        height, _ = self.stdscr.getmaxyx()
        page = max(1, height - 4)
        if key == ord("j") or _key_in(key, "KEY_DOWN"):
            self.transcript_scroll += 1
        elif key == ord("k") or _key_in(key, "KEY_UP"):
            self.transcript_scroll -= 1
        elif key in (ord("d"), ord(" ")) or _key_in(key, "KEY_NPAGE"):
            self.transcript_scroll += page
        elif key == ord("u") or _key_in(key, "KEY_PPAGE"):
            self.transcript_scroll -= page
        elif key == ord("g") or _key_in(key, "KEY_HOME"):
            self.transcript_scroll = 0
        elif key == ord("G") or _key_in(key, "KEY_END"):
            self.transcript_scroll = 10 ** 9
        elif key == ord("r"):
            self._reload_transcript()
        self.transcript_scroll = max(0, self.transcript_scroll)
        return True

    def handle_click(self, y: int, x: int) -> None:
        """Single click selects; a second click on the same target opens.

        Double-click timing is reported inconsistently across terminals, so
        the two-single-clicks convention is used instead.
        """
        if self.pending_prompt is not None or self.pending_confirm is not None:
            return
        if self.view == VIEW_GRID:
            for y1, x1, y2, x2, index in self.hitboxes:
                if y1 <= y <= y2 and x1 <= x <= x2:
                    if index == self.selected:
                        self.open_detail()
                    else:
                        self.selected = index
                    return
            return
        if self.view == VIEW_DETAIL:
            for row_y, index in self.detail_hitboxes:
                if y == row_y:
                    if index == self.detail_row:
                        self.open_transcript()
                    else:
                        self.detail_row = index
                    return

    # -- actions ------------------------------------------------------------

    def open_detail(self) -> None:
        if self.selected_session() is None:
            return
        self.view = VIEW_DETAIL
        self.detail_row = 0

    def open_transcript(self) -> None:
        session = self.selected_session()
        if session is None:
            return
        path: Path | None = None
        if self.detail_row == 0:
            path = session.transcript
        else:
            index = self.detail_row - 1
            if 0 <= index < len(session.subagents):
                path = session.subagents[index].path
        if not path:
            self.flash("No transcript on disk for this agent yet.")
            return
        self.transcript_path = path
        self.transcript_lines = transcript.render_transcript(path)
        self.view = VIEW_TRANSCRIPT
        # Open at the tail -- the end is the part you opened it to read.
        self.transcript_scroll = 10 ** 9

    def _reload_transcript(self) -> None:
        if self.transcript_path:
            self.transcript_lines = transcript.render_transcript(self.transcript_path)

    def rename_selected(self) -> None:
        session = self.selected_session()
        if session is None:
            return
        def commit(text: str) -> None:
            name = text.strip()
            discovery.save_custom_name(session.session_id, name)
            session.custom_name = name or None
            self.flash("Renamed." if name else "Name cleared.")
        self.start_prompt("Name", session.custom_name or "", commit)

    def stop_selected(self) -> None:
        session = self.selected_session()
        if session is None:
            return
        # Stopping asks first, always -- every row is a live process.
        self.ask(f"Stop {session.display_title}?", lambda: self._stop(session))

    def _stop(self, session) -> None:
        if session.kind == "background":
            try:
                done = subprocess.run(["claude", "stop", session.session_id],
                                      capture_output=True, text=True, timeout=15)
            except FileNotFoundError:
                self.flash("claude CLI not found on PATH.")
                return
            except subprocess.TimeoutExpired:
                self.flash("claude stop timed out.")
                return
            if getattr(done, "returncode", 1) == 0:
                self.flash("Stop requested; the conversation is kept and resumable.")
            else:
                detail = (getattr(done, "stderr", "") or "").strip()
                self.flash(_clip(f"claude stop failed: {detail}", 120) or "claude stop failed.")
        else:
            if not session.pid:
                self.flash("No pid to signal.")
                return
            try:
                os.kill(session.pid, signal.SIGTERM)
                self.flash("Sent SIGTERM; the transcript stays on disk.")
            except (OSError, ProcessLookupError) as error:
                self.flash(f"Could not signal {session.pid}: {error}")

    def attach_selected(self) -> None:
        session = self.selected_session()
        if session is None:
            return
        if session.kind == "background":
            self._attach_background(session)
            return
        host = terminal.host_of(session.pid) if session.pid else None
        if host == "Apple_Terminal":
            ok, message = terminal.focus(session.pid)
            self.flash(message)
            return
        friendly = terminal.describe_host(host)
        if session.status == "working":
            # Never fork a session that is actively working -- quietly ending
            # up on a divergent branch is worse than being told no.
            self.flash(f"Lives in {friendly}; it is working, and resuming would fork it. "
                       "Wait or stop it first.")
            return
        self.ask(
            f"Lives in {friendly}, which cannot be focused. Fork it with --resume? "
            "This copies the conversation.",
            lambda: self._resume(session),
        )

    def _attach_background(self, session) -> None:
        # `claude attach` takes the short job handle, never the session UUID
        # -- the UUID fails with `No job matching <uuid>`.
        if not session.job_id:
            self.flash("This background session has no job id to attach to.")
            return
        self._run_in_terminal(["claude", "attach", session.job_id])

    def _resume(self, session) -> None:
        self._run_in_terminal(["claude", "--resume", session.session_id])

    def _run_in_terminal(self, argv: list[str]) -> None:
        """Suspend curses, hand the terminal over, and restore on return."""
        try:
            curses.endwin()
        except curses.error:
            pass
        try:
            subprocess.call(argv)
        except FileNotFoundError:
            self.flash("claude CLI not found on PATH.")
        except Exception as error:
            self.flash(f"{argv[0]} failed: {error}")
        try:
            self.stdscr.keypad(True)
            curses.curs_set(0)
        except curses.error:
            pass
        # The next loop iteration redraws over whatever the child left behind.

    # -- the loop -----------------------------------------------------------

    def setup_curses(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.stdscr.keypad(True)
        # 250 ms so the spinner animates without input.
        self.stdscr.timeout(250)
        if hasattr(curses, "set_escdelay"):
            try:
                curses.set_escdelay(25)
            except curses.error:
                pass
        if not self.no_mouse:
            # Clicks only. Requesting motion/position events as well makes the
            # terminal's own text selection unusable; hold Option to select,
            # or launch with --no-mouse.
            try:
                curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
            except curses.error:
                pass
        init_colours()

    def run(self) -> None:
        self.setup_curses()
        self.poller.request()
        next_poll = time.time() + self.refresh_seconds
        while True:
            now = time.time()
            if now >= next_poll:
                self.poller.request()
                next_poll = now + self.refresh_seconds
            self.sync()
            self.draw()
            try:
                key = self.stdscr.getch()
            except KeyboardInterrupt:
                return
            if key == -1:
                continue
            if _key_in(key, "KEY_MOUSE"):
                try:
                    _, x, y, _, _ = curses.getmouse()
                except curses.error:
                    continue
                self.handle_click(y, x)
                continue
            if _key_in(key, "KEY_RESIZE"):
                continue
            if not self.handle_key(key):
                return


def main(options=None) -> None:
    """Entry point: wrap curses, swallow Ctrl-C on the way out."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    def _run(stdscr) -> None:
        AgentGrid(stdscr, options).run()
    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass
