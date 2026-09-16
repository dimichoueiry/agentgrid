"""Codex rollout discovery: one card per launch, and a pid for the terminal it lives in.

Codex 0.153+ writes helper threads (the "guardian" that judges risky actions)
as rollout files of their own next to the session that spawned them, in the
same cwd and stamped with the parent's start time. Read naively they were the
duplicate sessions one launch appeared to create. An interactive `codex` also
holds its rollout open, which is the only link from a card to the tab it runs
in -- and the reason a chat turn on that thread must be refused, not run.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from agentgrid import discovery, terminal, web

PARENT = "01a0a7d9-8599-7412-8d93-ae123ab93796"
GUARDIAN = "01a0a7d9-861b-7d43-b4e6-e8c04d0bb739"


def _meta(session_id, **extra):
    payload = {"id": session_id, "session_id": session_id, "cwd": "/repo",
               "originator": "codex-tui", "cli_version": "0.153.4",
               "source": "cli", "thread_source": "user"}
    payload.update(extra)
    return {"timestamp": "2026-09-16T01:34:05.000Z", "type": "session_meta", "payload": payload}


def _event(kind, **payload):
    return {"timestamp": "2026-09-16T01:34:08.000Z", "type": "event_msg",
            "payload": dict(type=kind, **payload)}


def _write(root, stamp, session_id, entries):
    today = date.today()
    directory = root / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{stamp}-{session_id}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


class SubagentRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for target in (mock.patch.object(discovery, "CODEX_SESSIONS_DIR", self.root),
                       mock.patch.object(discovery, "codex_titles", lambda: {}),
                       mock.patch.object(terminal, "codex_terminal_pids", lambda: []),
                       mock.patch.object(terminal, "rollouts_open_by", lambda pids: {})):
            target.start()
            self.addCleanup(target.stop)

    def test_a_guardian_rollout_is_not_a_second_session(self):
        # Real shapes from codex-cli 0.153.4: the guardian file carries the
        # parent's start time in its name and the parent's cwd inside.
        _write(self.root, "2026-09-16T01-34-05", GUARDIAN, [
            _meta(GUARDIAN, parent_thread_id=PARENT, thread_source="guardian_review",
                  source={"subagent": {"other": "guardian"}}),
            _event("task_started"), _event("task_complete"),
        ])
        _write(self.root, "2026-09-16T01-34-05", PARENT, [
            _meta(PARENT), _event("task_started"),
        ])
        sessions = discovery.collect_codex(discovery.CodexCache())
        self.assertEqual([s.session_id for s in sessions], [PARENT])
        self.assertEqual(sessions[0].engine, "codex")

    def test_a_parent_id_alone_marks_a_helper_only_with_a_non_user_source(self):
        self.assertTrue(discovery.is_subagent_meta(
            {"parent_thread_id": PARENT, "thread_source": "guardian_review"}))
        self.assertTrue(discovery.is_subagent_meta({"source": {"subagent": {"other": "x"}}}))
        # A user's own thread, a forked one, and an older CLI's meta all stay.
        self.assertFalse(discovery.is_subagent_meta({"parent_thread_id": PARENT, "thread_source": "user"}))
        self.assertFalse(discovery.is_subagent_meta({"source": "exec", "thread_source": "user"}))
        self.assertFalse(discovery.is_subagent_meta({}))

    def test_a_held_name_lands_on_the_users_thread_not_the_helper(self):
        # Glob order put the guardian first, and it matched cwd + time too.
        helper = "01a0a7d9-0000-0000-0000-000000000001"
        _write(self.root, "2026-09-16T01-34-05", helper, [
            _meta(helper, parent_thread_id=PARENT, thread_source="guardian_review",
                  source={"subagent": {"other": "guardian"}}),
        ])
        _write(self.root, "2026-09-16T01-34-05", PARENT, [_meta(PARENT)])
        sessions = discovery.collect_codex(discovery.CodexCache())
        for session in sessions:
            session.started_at = int(time.time() * 1000)
        fleet = web.Fleet()
        fleet.name_codex_when_seen("/repo", "Fix the tests")
        saved = {}
        with mock.patch.object(discovery, "save_custom_name",
                               lambda sid, name: saved.__setitem__(sid, name)):
            fleet._apply_pending_names(sessions)
        self.assertEqual(saved, {PARENT: "Fix the tests"})

    def test_the_tui_prompt_is_read_from_the_completed_user_message(self):
        _write(self.root, "2026-09-16T01-34-05", PARENT, [
            _meta(PARENT),
            _event("item_completed", item={"type": "UserMessage", "id": "u1", "content": [
                {"type": "local_image", "path": "/tmp/board.png"},
                {"type": "text", "text": "Make the board two columns"}]}),
            _event("item_completed", item={"type": "AgentMessage", "content": [
                {"type": "text", "text": "Sure"}]}),
        ])
        (session,) = discovery.collect_codex(discovery.CodexCache())
        self.assertEqual(session.last_prompt, "Make the board two columns")


class TerminalOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for target in (mock.patch.object(discovery, "CODEX_SESSIONS_DIR", self.root),
                       mock.patch.object(discovery, "codex_titles", lambda: {})):
            target.start()
            self.addCleanup(target.stop)

    def test_a_rollout_held_open_by_a_terminal_codex_gets_that_pid(self):
        path = _write(self.root, "2026-09-16T01-34-05", PARENT, [_meta(PARENT)])
        with mock.patch.object(terminal, "codex_terminal_pids", lambda: [41614]), \
             mock.patch.object(terminal, "rollouts_open_by", lambda pids: {str(path): 41614}):
            (session,) = discovery.collect_codex(discovery.CodexCache())
        self.assertEqual(session.pid, 41614)
        self.assertEqual(session.kind, "interactive")
        self.assertTrue(web.Fleet._as_json(session)["openable"])

    def test_a_headless_rollout_has_no_pid_and_is_not_openable(self):
        _write(self.root, "2026-09-16T01-34-05", PARENT,
               [_meta(PARENT, originator="codex_exec", source="exec")])
        with mock.patch.object(terminal, "codex_terminal_pids", lambda: []), \
             mock.patch.object(terminal, "rollouts_open_by", lambda pids: {}):
            (session,) = discovery.collect_codex(discovery.CodexCache())
        self.assertIsNone(session.pid)
        self.assertFalse(web.Fleet._as_json(session)["openable"])

    def test_process_scans_are_throttled_and_lsof_runs_only_on_a_change(self):
        scans, lsofs = [], []
        cache = discovery.CodexCache()
        live = [7]
        with mock.patch.object(terminal, "codex_terminal_pids", lambda: scans.append(1) or list(live)), \
             mock.patch.object(terminal, "rollouts_open_by", lambda pids: lsofs.append(pids) or {}):
            cache.terminal_owners(1000.0)
            cache.terminal_owners(1002.0)          # two polls later: no ps, no lsof
            self.assertEqual((len(scans), lsofs), (1, [[7]]))
            cache.terminal_owners(1006.0)          # scan again, same set: no lsof
            self.assertEqual((len(scans), lsofs), (2, [[7]]))
            live.append(8)
            cache.terminal_owners(1012.0)          # a new terminal codex: lsof once more
            self.assertEqual(lsofs, [[7], [7, 8]])
            live.clear()
            cache.terminal_owners(1018.0)          # none left: nothing to ask lsof about
            self.assertEqual(lsofs, [[7], [7, 8]])
            self.assertEqual(cache.terminal_owners(1018.0), {})


class TerminalHelperTests(unittest.TestCase):
    def test_terminal_codex_processes_are_found_by_tty_and_command(self):
        ps = ("41614 ttys012 /Applications/Codex.app/Contents/Resources/codex -m gpt-6 do it\n"
              "  2414 ??      /Applications/Codex.app/Contents/Resources/codex app-server\n"
              " 9001 ??      /opt/codex exec resume --json -- id hello\n"
              "17188 ttys003 claude --model x\n"
              "  501 ttys004 /usr/bin/codexify --flag\n")
        with mock.patch.object(terminal, "_run", return_value=mock.Mock(returncode=0, stdout=ps)):
            self.assertEqual(terminal.codex_terminal_pids(), [41614])
        with mock.patch.object(terminal, "_run", return_value=None):
            self.assertEqual(terminal.codex_terminal_pids(), [])

    def test_lsof_fields_map_rollouts_to_their_pid(self):
        out = ("p17188\nfcwd\nn/Users/me/AgentGrid\n"
               "p41614\nfcwd\nn/Users/me/AgentGrid\n"
               "f3\nn/Users/me/.codex/sessions/2026/09/15/rollout-2026-09-15T21-34-05-abc.jsonl\n"
               "f4\nn/Users/me/.codex/sessions/2026/09/15/rollout-2026-09-15T21-53-13-guardian.jsonl\n"
               "f5\nn/Users/me/.codex/log/codex-tui.log\n")
        with mock.patch.object(terminal, "_run", return_value=mock.Mock(returncode=1, stdout=out)) as run:
            owners = terminal.rollouts_open_by([17188, 41614])
        self.assertEqual(owners, {
            "/Users/me/.codex/sessions/2026/09/15/rollout-2026-09-15T21-34-05-abc.jsonl": 41614,
            "/Users/me/.codex/sessions/2026/09/15/rollout-2026-09-15T21-53-13-guardian.jsonl": 41614,
        })
        self.assertIn("17188,41614", run.call_args.args[0])
        self.assertEqual(terminal.rollouts_open_by([]), {})

    def test_focus_accepts_a_codex_process(self):
        with mock.patch.object(terminal, "process_command",
                               return_value="/Applications/Codex.app/Contents/Resources/codex -m x hi"), \
             mock.patch.object(terminal, "tty_of", return_value="/dev/ttys012"), \
             mock.patch.object(terminal, "host_of", return_value="Apple_Terminal"), \
             mock.patch.object(terminal, "focus_tty",
                               return_value=(True, "Focused the Terminal tab.")) as focus:
            ok, message = terminal.focus(41614)
        self.assertTrue(ok)
        focus.assert_called_once_with("/dev/ttys012")
        with mock.patch.object(terminal, "process_command", return_value="/usr/bin/codexify"):
            ok, message = terminal.focus(41614)
        self.assertFalse(ok)
        self.assertIn("different process", message)


if __name__ == "__main__":
    unittest.main()
