"""Layout and logic tests over a hand-written fake curses window.

The point of this harness: the real drawing code runs unmodified against a
fake window and the resulting character grid is asserted on, so a
card-geometry regression fails here rather than only being visible to a human
squinting at a screen. `curses` itself is replaced with a MagicMock whose
`error` is a stand-in exception -- which is also why the drawing code probes
colour support defensively; under test, any comparison against COLORS raises.

Nothing in here may touch the real home directory: every state-file test
patches the module-level path constant to a temporary location first.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import discovery, terminal, ui

try:
    from agentgrid import notes
except Exception:   # notes.py may land after this file in the build order
    notes = None

# Corner assertions accept either border weight: the selected card draws heavy
# rules, everything else light ones.
TOP_CORNERS = ("╭", "┏")
BOTTOM_CORNERS = ("╰", "┗")


class _CursesError(Exception):
    """Stands in for curses.error under the MagicMock."""


class FakeScreen:
    """Minimal stand-in for a curses window.

    `addstr` raises on any out-of-bounds write, exactly the discipline the
    real library enforces -- so a drawing path that escapes the clipping
    helper fails loudly here.
    """

    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.buf = [[" "] * cols for _ in range(rows)]

    def getmaxyx(self):
        return self.rows, self.cols

    def addstr(self, y, x, text, attr=0):
        if not (0 <= y < self.rows):
            raise _CursesError("out of bounds")
        for index, character in enumerate(text):
            if x + index >= self.cols:
                raise _CursesError("out of bounds")
            self.buf[y][x + index] = character

    def erase(self):
        self.buf = [[" "] * self.cols for _ in range(self.rows)]

    def noutrefresh(self):
        pass

    def keypad(self, _):
        pass

    def timeout(self, _):
        pass

    def lines(self):
        return ["".join(row).rstrip() for row in self.buf]


def _fake_curses() -> mock.MagicMock:
    fake = mock.MagicMock()
    fake.error = _CursesError
    fake.color_pair = mock.MagicMock(return_value=0)
    return fake


class _StubPoller:
    """Records refresh requests without ever spawning a thread."""

    def __init__(self) -> None:
        self.requests = 0

    def request(self) -> None:
        self.requests += 1

    def take(self):
        return None


def make_session(**overrides):
    defaults = dict(
        session_id="aaaabbbb-1111-2222-3333-444455556666",
        kind="interactive",
        status="idle",
        cwd="/tmp/repo",
        started_at=int(time.time() * 1000),
    )
    defaults.update(overrides)
    return discovery.Session(**defaults)


def make_subagent(index: int = 0, status: str = "working"):
    return discovery.SubAgent(
        agent_id=f"agent-{index}",
        description=f"find the ranking code {index}",
        agent_type="Explore",
        status=status,
        messages=10 + index,
    )


class GridCase(unittest.TestCase):
    """Base: a real AgentGrid around a FakeScreen, curses fully mocked."""

    def build(self, rows=24, cols=100, sessions=(), options=None):
        patcher = mock.patch.object(ui, "curses", _fake_curses())
        patcher.start()
        self.addCleanup(patcher.stop)
        screen = FakeScreen(rows, cols)
        grid = ui.AgentGrid(screen, options)
        grid.sessions = list(sessions)
        grid.loaded = True
        grid.poller = _StubPoller()
        return grid, screen

    @staticmethod
    def corners(lines, kinds):
        found = []
        for y, line in enumerate(lines):
            for x, character in enumerate(line):
                if character in kinds:
                    found.append((y, x))
        return found

    def card_height_at(self, lines, top):
        y0, x0 = top
        for y in range(y0 + 1, len(lines)):
            line = lines[y]
            if x0 < len(line) and line[x0] in BOTTOM_CORNERS:
                return y - y0 + 1
        self.fail(f"no bottom corner below {top}")


# ---------------------------------------------------------------------------
# Card geometry


class CardGeometryTests(GridCase):
    def test_height_collapses_without_subagents(self):
        grid, screen = self.build(rows=30, cols=60, sessions=[make_session()])
        grid.draw_grid()
        lines = screen.lines()
        tops = self.corners(lines, TOP_CORNERS)
        self.assertEqual(len(tops), 1)
        self.assertEqual(self.card_height_at(lines, tops[0]), 6)

    def test_height_grows_with_subagents(self):
        session = make_session(subagents=[make_subagent(0), make_subagent(1)])
        grid, screen = self.build(rows=30, cols=60, sessions=[session])
        grid.draw_grid()
        lines = screen.lines()
        tops = self.corners(lines, TOP_CORNERS)
        self.assertEqual(self.card_height_at(lines, tops[0]), 8)

    def test_height_caps_at_three_subagent_rows(self):
        session = make_session(subagents=[make_subagent(i) for i in range(5)])
        grid, screen = self.build(rows=30, cols=60, sessions=[session])
        grid.draw_grid()
        lines = screen.lines()
        tops = self.corners(lines, TOP_CORNERS)
        self.assertEqual(self.card_height_at(lines, tops[0]), 9)

    def test_every_card_shares_the_busiest_height(self):
        quiet = make_session(session_id="quiet-1")
        busy = make_session(session_id="busy-1",
                            subagents=[make_subagent(0), make_subagent(1)])
        grid, screen = self.build(rows=30, cols=60, sessions=[quiet, busy])
        grid.draw_grid()
        lines = screen.lines()
        tops = sorted(self.corners(lines, TOP_CORNERS))
        self.assertEqual(len(tops), 2)
        heights = {self.card_height_at(lines, top) for top in tops}
        self.assertEqual(heights, {8})

    def test_every_card_has_a_closing_border(self):
        sessions = [make_session(session_id=f"s{i}-0000") for i in range(4)]
        grid, screen = self.build(rows=40, cols=60, sessions=sessions)
        grid.draw_grid()
        lines = screen.lines()
        self.assertEqual(len(self.corners(lines, TOP_CORNERS)), 4)
        self.assertEqual(len(self.corners(lines, BOTTOM_CORNERS)), 4)

    def test_second_row_cards_are_complete(self):
        sessions = [make_session(session_id=f"s{i}-0000") for i in range(3)]
        grid, screen = self.build(rows=24, cols=100, sessions=sessions)
        grid.draw_grid()
        lines = screen.lines()
        tops = sorted(self.corners(lines, TOP_CORNERS))
        self.assertEqual(len(tops), 3)
        # Two columns fit at 100 wide, so the third card sits on a second row
        # and must still be fully framed.
        rows = {y for y, _ in tops}
        self.assertEqual(len(rows), 2)
        for top in tops:
            self.card_height_at(lines, top)   # fails if any bottom is missing

    def test_cards_never_overlap_vertically(self):
        sessions = [make_session(session_id=f"s{i}-0000") for i in range(3)]
        grid, screen = self.build(rows=30, cols=60, sessions=sessions)
        grid.draw_grid()
        lines = screen.lines()
        tops = sorted(self.corners(lines, TOP_CORNERS))
        for (y_first, x_first), (y_second, x_second) in zip(tops, tops[1:]):
            if x_first == x_second:
                height = self.card_height_at(lines, (y_first, x_first))
                self.assertGreaterEqual(y_second, y_first + height)

    def test_narrow_terminal_falls_back_to_one_column(self):
        sessions = [make_session(session_id=f"s{i}-0000") for i in range(3)]
        grid, screen = self.build(rows=24, cols=60, sessions=sessions)
        grid.draw_grid()
        columns = {x for _, x in self.corners(screen.lines(), TOP_CORNERS)}
        self.assertEqual(len(columns), 1)

    def test_drawing_never_exceeds_terminal_bounds(self):
        # A long everything, plus subagents -- and screens down to 6 rows.
        session = make_session(
            title="an extremely long title " * 8,
            git_branch="exp/dec/a-very-long-branch-name-that-would-overflow",
            last_prompt="do the thing " * 40,
            subagents=[make_subagent(i) for i in range(5)],
            tool_counts={"Bash": 25, "Read": 400, "Write": 2, "Grep": 9},
        )
        for rows, cols in ((24, 100), (10, 44), (6, 50), (50, 200), (24, 60), (5, 45)):
            grid, screen = self.build(rows=rows, cols=cols,
                                      sessions=[session, make_session(session_id="b-1")])
            grid.draw_grid()
            grid.draw_footer()
            self.assertTrue(all(len(row) == cols for row in screen.buf))


# ---------------------------------------------------------------------------
# Status model


class StatusTests(unittest.TestCase):
    def test_blocked_sorts_first(self):
        sessions = [
            make_session(session_id="i-1", status="idle", last_activity=500.0),
            make_session(session_id="b-1", status="blocked", last_activity=10.0),
            make_session(session_id="w-1", status="working", last_activity=900.0),
        ]
        ordered = sorted(sessions, key=lambda session: session.sort_key)
        self.assertEqual(ordered[0].status, "blocked")

    def test_normalize_status_handles_both_vocabularies(self):
        self.assertEqual(discovery.normalize_status({"state": "working"}), "working")
        self.assertEqual(discovery.normalize_status({"state": "blocked"}), "blocked")
        # `state` wins even when a stray `status` rides along.
        self.assertEqual(
            discovery.normalize_status({"state": "done", "status": "busy"}), "done")
        self.assertEqual(discovery.normalize_status({"status": "busy"}), "working")
        self.assertEqual(discovery.normalize_status({"status": "idle"}), "idle")
        # An interactive session blocked on a question or permission prompt
        # reports `waiting`. Unmapped it matched no column and the card
        # vanished from the board exactly while it needed a human.
        self.assertEqual(discovery.normalize_status({"status": "waiting"}), "blocked")
        self.assertEqual(discovery.normalize_status({}), "unknown")


# ---------------------------------------------------------------------------
# --cwd scoping


class CwdFilterTests(unittest.TestCase):
    def test_sibling_with_shared_prefix_is_excluded(self):
        with tempfile.TemporaryDirectory() as base:
            repo = Path(base) / "repo"
            sibling = Path(base) / "repo-tests"
            inside = repo / "src"
            inside.mkdir(parents=True)
            sibling.mkdir()
            self.assertTrue(ui._is_within(str(repo), str(repo)))
            self.assertTrue(ui._is_within(str(inside), str(repo)))
            # A string-prefix comparison would pass this; components must not.
            self.assertFalse(ui._is_within(str(sibling), str(repo)))

    def test_nonexistent_path_is_not_within(self):
        with tempfile.TemporaryDirectory() as base:
            repo = Path(base) / "repo"
            repo.mkdir()
            self.assertFalse(ui._is_within("/no/such/path/anywhere", str(repo)))


# ---------------------------------------------------------------------------
# Confirmations


class ConfirmationTests(GridCase):
    def test_action_runs_only_on_y(self):
        grid, _ = self.build(sessions=[make_session()])
        ran = []
        grid.ask("Sure?", lambda: ran.append(True))
        grid.handle_key(ord("y"))
        self.assertEqual(ran, [True])

    def test_any_other_key_cancels(self):
        grid, _ = self.build(sessions=[make_session()])
        ran = []
        for key in (ord("n"), 27, ord("x"), ord("Y")):
            grid.ask("Sure?", lambda: ran.append(key))
            if key == ord("Y"):
                continue   # exercised in its own assertion below
            grid.pending_confirm = {"message": "Sure?", "action": lambda: ran.append(True)}
            grid.handle_key(key if key != ord("Y") else ord("n"))
        grid.pending_confirm = None
        self.assertNotIn(True, ran)

    def test_capital_y_also_confirms(self):
        grid, _ = self.build(sessions=[make_session()])
        ran = []
        grid.ask("Sure?", lambda: ran.append(True))
        grid.handle_key(ord("Y"))
        self.assertEqual(ran, [True])

    def test_stop_asks_before_signalling(self):
        session = make_session(kind="background", status="working",
                               job_id="deadbeef")
        grid, _ = self.build(sessions=[session])
        fake_subprocess = mock.MagicMock()
        fake_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(ui, "subprocess", fake_subprocess):
            grid.stop_selected()
            self.assertIsNotNone(grid.pending_confirm)
            self.assertFalse(fake_subprocess.run.called)
            grid.handle_key(ord("y"))
            self.assertTrue(fake_subprocess.run.called)
            argv = fake_subprocess.run.call_args[0][0]
            self.assertEqual(argv[:2], ["claude", "stop"])
            self.assertEqual(argv[2], session.session_id)

    def test_declining_never_signals(self):
        session = make_session(kind="background", job_id="deadbeef")
        grid, _ = self.build(sessions=[session])
        fake_subprocess = mock.MagicMock()
        with mock.patch.object(ui, "subprocess", fake_subprocess):
            grid.stop_selected()
            grid.handle_key(ord("n"))
            self.assertFalse(fake_subprocess.run.called)
            self.assertIsNone(grid.pending_confirm)


# ---------------------------------------------------------------------------
# Clicks


class ClickTests(GridCase):
    def _grid_of_four(self):
        sessions = [make_session(session_id=f"s{i}-0000") for i in range(4)]
        grid, screen = self.build(rows=24, cols=100, sessions=sessions)
        grid.draw_grid()
        return grid, screen

    def test_hitboxes_cover_every_drawn_card(self):
        grid, screen = self._grid_of_four()
        drawn = self.corners(screen.lines(), TOP_CORNERS)
        self.assertEqual(len(grid.hitboxes), len(drawn))
        # Every drawn top corner falls inside exactly one hitbox.
        for y, x in drawn:
            containing = [box for box in grid.hitboxes
                          if box[0] <= y <= box[2] and box[1] <= x <= box[3]]
            self.assertEqual(len(containing), 1)

    def test_click_selects_the_card_under_the_cursor(self):
        grid, _ = self._grid_of_four()
        y1, x1, y2, x2, index = grid.hitboxes[2]
        grid.handle_click((y1 + y2) // 2, (x1 + x2) // 2)
        self.assertEqual(grid.selected, index)
        self.assertEqual(grid.view, ui.VIEW_GRID)

    def test_second_click_opens(self):
        grid, _ = self._grid_of_four()
        y1, x1, y2, x2, _ = grid.hitboxes[1]
        middle = ((y1 + y2) // 2, (x1 + x2) // 2)
        grid.handle_click(*middle)
        grid.handle_click(*middle)
        self.assertEqual(grid.view, ui.VIEW_DETAIL)

    def test_click_on_empty_space_changes_nothing(self):
        grid, _ = self._grid_of_four()
        grid.selected = 3
        grid.handle_click(0, 0)   # header row: never a card
        self.assertEqual(grid.selected, 3)
        self.assertEqual(grid.view, ui.VIEW_GRID)

    def test_clicks_land_on_the_card_they_visually_overlap(self):
        grid, screen = self._grid_of_four()
        # Click the exact top-left corner glyph of each drawn card and check
        # the selection matches the hitbox that owns that pixel.
        for y1, x1, _, _, index in grid.hitboxes:
            character = screen.lines()[y1][x1]
            self.assertIn(character, TOP_CORNERS)
            grid.view = ui.VIEW_GRID
            grid.selected = -1 if index != 0 else 1   # never pre-selected
            grid.handle_click(y1, x1)
            self.assertEqual(grid.selected, index)


# ---------------------------------------------------------------------------
# Terminal focus


class TerminalFocusTests(unittest.TestCase):
    def test_non_scriptable_host_reports_why(self):
        with mock.patch.object(terminal, "host_of", return_value="vscode"), \
             mock.patch.object(terminal, "tty_of", return_value="/dev/ttys004"):
            ok, message = terminal.focus(4242)
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_tty_is_normalised_to_the_dev_path(self):
        fake = mock.MagicMock()
        fake.run.return_value = SimpleNamespace(returncode=0, stdout="ttys004\n", stderr="")
        with mock.patch.object(terminal, "subprocess", fake):
            self.assertEqual(terminal.tty_of(4242), "/dev/ttys004")

    def test_missing_tty_is_reported_not_crashed(self):
        fake = mock.MagicMock()
        fake.run.return_value = SimpleNamespace(returncode=0, stdout="??\n", stderr="")
        with mock.patch.object(terminal, "subprocess", fake):
            self.assertIsNone(terminal.tty_of(4242))
        with mock.patch.object(terminal, "host_of", return_value="Apple_Terminal"), \
             mock.patch.object(terminal, "tty_of", return_value=None):
            ok, message = terminal.focus(4242)
        self.assertFalse(ok)
        self.assertTrue(message)


# ---------------------------------------------------------------------------
# Renaming


class RenameTests(GridCase):
    def test_custom_name_wins_over_ai_title(self):
        session = make_session(custom_name="Fleet check", title="Investigate ticket")
        self.assertEqual(session.display_title, "Fleet check")

    def test_four_source_fallback_order(self):
        session = make_session(session_id="12345678-aaaa", custom_name="A",
                               title="B", name="C")
        self.assertEqual(session.display_title, "A")
        session.custom_name = None
        self.assertEqual(session.display_title, "B")
        session.title = None
        self.assertEqual(session.display_title, "C")
        session.name = None
        self.assertEqual(session.display_title, "12345678")

    def test_save_and_clear_round_trip(self):
        with tempfile.TemporaryDirectory() as base:
            names_path = Path(base) / "names.json"
            with mock.patch.object(discovery, "NAMES_PATH", names_path):
                discovery.save_custom_name("id-1", "Fleet check")
                self.assertEqual(discovery.load_custom_names(), {"id-1": "Fleet check"})
                discovery.save_custom_name("id-1", "")
                self.assertEqual(discovery.load_custom_names(), {})

    def test_corrupt_names_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as base:
            names_path = Path(base) / "names.json"
            names_path.write_text("{not json at all", encoding="utf-8")
            with mock.patch.object(discovery, "NAMES_PATH", names_path):
                self.assertEqual(discovery.load_custom_names(), {})

    def test_typing_a_name_and_pressing_enter_saves_it(self):
        session = make_session()
        grid, _ = self.build(sessions=[session])
        with mock.patch.object(discovery, "save_custom_name") as save:
            grid.handle_key(ord("R"))
            self.assertIsNotNone(grid.pending_prompt)
            grid.handle_key(ord("a"))
            grid.handle_key(ord("b"))
            grid.handle_key(10)
            save.assert_called_once_with(session.session_id, "ab")
        self.assertEqual(session.custom_name, "ab")

    def test_escape_cancels_without_saving(self):
        session = make_session()
        grid, _ = self.build(sessions=[session])
        with mock.patch.object(discovery, "save_custom_name") as save:
            grid.handle_key(ord("R"))
            grid.handle_key(ord("z"))
            grid.handle_key(27)
            save.assert_not_called()
        self.assertIsNone(grid.pending_prompt)

    def test_q_is_typed_rather_than_quitting_while_naming(self):
        grid, _ = self.build(sessions=[make_session()])
        grid.handle_key(ord("R"))
        # A rename prompt that does not swallow q quits the app mid-name.
        keep_running = grid.handle_key(ord("q"))
        self.assertTrue(keep_running)
        self.assertEqual(grid.pending_prompt["text"], "q")

    def test_uppercase_r_renames_rather_than_refreshing(self):
        grid, _ = self.build(sessions=[make_session()])
        grid.handle_key(ord("R"))
        self.assertIsNotNone(grid.pending_prompt)
        self.assertEqual(grid.poller.requests, 0)

    def test_lowercase_r_refreshes(self):
        grid, _ = self.build(sessions=[make_session()])
        grid.handle_key(ord("r"))
        self.assertIsNone(grid.pending_prompt)
        self.assertEqual(grid.poller.requests, 1)


# ---------------------------------------------------------------------------
# The hook's waiting state


class WaitingStateTests(unittest.TestCase):
    @staticmethod
    def _apply(session, waiting):
        # Tolerate a mutating implementation that returns None.
        return discovery.apply_waiting_state(session, waiting) or session

    def test_idle_interactive_session_is_promoted_to_blocked(self):
        session = make_session(status="idle", kind="interactive", last_activity=100.0)
        waiting = {session.session_id: {"waiting": True, "since": 200.0, "reason": "perm"}}
        self.assertEqual(self._apply(session, waiting).status, "blocked")

    def test_busy_session_is_never_overridden(self):
        session = make_session(status="working", kind="interactive", last_activity=100.0)
        waiting = {session.session_id: {"waiting": True, "since": 200.0, "reason": ""}}
        self.assertEqual(self._apply(session, waiting).status, "working")

    def test_activity_after_the_event_clears_the_wait(self):
        session = make_session(status="idle", kind="interactive", last_activity=300.0)
        waiting = {session.session_id: {"waiting": True, "since": 200.0, "reason": ""}}
        self.assertEqual(self._apply(session, waiting).status, "idle")

    def test_cleared_entry_leaves_status_alone(self):
        session = make_session(status="idle", kind="interactive", last_activity=100.0)
        waiting = {session.session_id: {"waiting": False, "since": 200.0, "reason": ""}}
        self.assertEqual(self._apply(session, waiting).status, "idle")

    def test_background_sessions_are_untouched(self):
        session = make_session(status="idle", kind="background", last_activity=100.0)
        waiting = {session.session_id: {"waiting": True, "since": 200.0, "reason": ""}}
        self.assertEqual(self._apply(session, waiting).status, "idle")

    def test_missing_state_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as base:
            absent = Path(base) / "state.json"
            with mock.patch.object(discovery, "HOOK_STATE_PATH", absent):
                self.assertEqual(discovery.load_waiting_state(), {})


# ---------------------------------------------------------------------------
# Incremental transcript parsing


class TranscriptCacheTests(unittest.TestCase):
    def test_incremental_parse_picks_up_appended_lines(self):
        cache = discovery.TranscriptCache()
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "session.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "ai-title", "aiTitle": "first"}) + "\n")
            state = cache.parse(path)
            self.assertEqual(state["title"], "first")
            offset_after_first = state["offset"]
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "ai-title", "aiTitle": "second"}) + "\n")
            state = cache.parse(path)
            self.assertEqual(state["title"], "second")
            self.assertGreater(state["offset"], offset_after_first)

    def test_partial_trailing_line_is_deferred(self):
        cache = discovery.TranscriptCache()
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "session.jsonl"
            complete = json.dumps({"type": "ai-title", "aiTitle": "kept"}) + "\n"
            partial = '{"type": "ai-title", "aiTitle": "torn'
            path.write_text(complete + partial, encoding="utf-8")
            state = cache.parse(path)
            # The torn line is a session mid-write; it must not be consumed.
            self.assertEqual(state["title"], "kept")
            self.assertEqual(state["offset"], len(complete.encode("utf-8")))
            with path.open("a", encoding="utf-8") as handle:
                handle.write('"}\n')
            state = cache.parse(path)
            self.assertEqual(state["title"], "torn")


# ---------------------------------------------------------------------------
# Notes: due dates and the slot-pouring reorder (the two gaps the original
# test suite left open)


@unittest.skipIf(notes is None, "notes.py is not on disk yet")
class NotesTests(unittest.TestCase):
    ANCHOR = date(2026, 8, 3)   # a Monday

    def _due(self, text):
        result = notes.parse_due(text, self.ANCHOR)
        if isinstance(result, tuple):
            result = result[0]
        if hasattr(result, "isoformat"):
            result = result.isoformat()
        return result

    def test_tomorrow_resolves_against_the_anchor(self):
        self.assertEqual(self._due("chase kamil tomorrow"), "2026-08-04")

    def test_in_n_days(self):
        self.assertEqual(self._due("ship it in 3 days"), "2026-08-06")

    def test_month_day_rolls_forward_a_year(self):
        # March has already passed the August anchor, so it means NEXT March.
        self.assertEqual(self._due("march 3rd"), "2027-03-03")

    def test_tagged_weekday_counts(self):
        self.assertEqual(self._due("standup notes #fri"), "2026-08-07")

    def test_cued_weekday_counts(self):
        self.assertEqual(self._due("send the doc on friday"), "2026-08-07")

    def test_weekday_is_always_forward_never_today(self):
        # #mon on a Monday means NEXT Monday.
        self.assertEqual(self._due("#mon"), "2026-08-10")

    def test_bare_weekday_is_not_a_date(self):
        self.assertIsNone(self._due("wednesday sync"))

    def test_impossible_date_is_not_a_date(self):
        self.assertIsNone(self._due("february 30"))

    def test_prose_containing_march_is_not_a_date(self):
        self.assertIsNone(self._due("the long march of progress"))

    def test_iso_date(self):
        self.assertEqual(self._due("due 2026-12-01"), "2026-12-01")

    def test_reorder_keeps_absent_pages_in_place(self):
        with tempfile.TemporaryDirectory() as base:
            notes_dir = Path(base) / "notes"
            meta_path = Path(base) / "note-meta.json"
            day = "2026-08-04"   # a Tuesday
            day_dir = notes_dir / day
            day_dir.mkdir(parents=True)
            # B is a Monday-only page: in the global order, absent today.
            for name in ("A", "C", "D"):
                (day_dir / f"{name}.md").write_text(f"# {name}\n\n", encoding="utf-8")
            with mock.patch.object(notes, "NOTES_DIR", notes_dir), \
                 mock.patch.object(notes, "META_PATH", meta_path):
                notes.save_meta({"order": ["A", "B", "C", "D"], "groups": {}})
                notes.set_order(day, ["D", "A", "C"])
                order = notes.load_meta().get("order")
            # Today's pages poured into today's slots; B holds its position.
            self.assertEqual(order, ["D", "B", "A", "C"])


if __name__ == "__main__":
    unittest.main()
