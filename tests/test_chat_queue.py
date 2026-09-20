"""The chat queue: messages sent mid-turn wait, visible and editable, until they start."""
import io
import queue as queuelib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import chat, web


class QueueTests(unittest.TestCase):
    def setUp(self):
        # The queue is written through to disk; keep the real one out of it.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patch = mock.patch.object(chat, "QUEUE_DIR", Path(folder.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.room = chat.ChatSession("sid", "/repo", "claude")
        self.ran = []
        self.holding = threading.Event()
        self.release = threading.Event()

        # Stands in for the CLI: records what was really sent, and holds the
        # first turn open until the test lets it finish.
        def fake_turn(message, posture, model="", attachments=None):
            self.ran.append(message)
            self.holding.set()
            self.release.wait(5)

        self.room._run_turn = fake_turn
        self.addCleanup(self.release.set)

    def busy(self):
        first = self.room.send("first", "auto")
        self.assertTrue(self.holding.wait(5), "the first turn never started")
        return first

    def drain(self):
        self.release.set()
        worker = self.room._worker
        if worker:
            worker.join(5)

    def messages(self):
        return [q["message"] for q in self.room.state()["queue"]]

    def test_a_send_mid_turn_waits_and_is_visible(self):
        first = self.busy()
        self.assertFalse(first["queued"])
        second = self.room.send("second", "read-only", "opus",
                                ["/h/.agentgrid/uploads/sid/20260918-101010-abc123-shot.png"])
        self.assertTrue(second["queued"])
        state = self.room.state()
        self.assertEqual(state["queued"], 1)
        self.assertEqual(state["queue"], [{
            "id": second["id"], "message": "second", "posture": "read-only", "model": "opus",
            "files": ["20260918-101010-abc123-shot.png"], "held": ""}])

    def test_an_edit_changes_the_message_that_is_sent(self):
        self.busy()
        waiting = self.room.send("draft", "auto")
        self.assertTrue(self.room.edit_queued(waiting["id"], "  final words  "))
        self.assertEqual(self.messages(), ["final words"])
        self.drain()
        self.assertEqual(self.ran, ["first", "final words"])

    def test_a_started_message_can_no_longer_be_edited(self):
        first = self.busy()
        self.assertFalse(self.room.edit_queued(first["id"], "too late"))
        self.assertFalse(self.room.remove_queued(first["id"]))
        self.drain()
        self.assertEqual(self.ran, ["first"])

    def test_an_edit_may_not_empty_a_message_with_nothing_else_to_send(self):
        self.busy()
        words = self.room.send("words", "auto")
        picture = self.room.send("look", "auto", "", ["/u/a.png"])
        with self.assertRaises(ValueError):
            self.room.edit_queued(words["id"], "   ")
        self.assertTrue(self.room.edit_queued(picture["id"], ""))
        self.assertEqual(self.messages(), ["words", ""])

    def test_remove_drops_it_and_restore_puts_it_back_in_place(self):
        self.busy()
        a, b, c = (self.room.send(m, "auto") for m in "abc")
        self.assertTrue(self.room.remove_queued(b["id"]))
        self.assertEqual(self.messages(), ["a", "c"])
        self.assertEqual(self.room.state()["queued"], 2)
        self.assertTrue(self.room.restore_queued(b["id"]))
        self.assertEqual(self.messages(), ["a", "b", "c"])
        self.assertFalse(self.room.restore_queued(b["id"]), "one undo per remove")
        self.drain()
        self.assertEqual(self.ran, ["first", "a", "b", "c"])

    def test_move_reorders_what_is_sent(self):
        self.busy()
        a, b, c = (self.room.send(m, "auto") for m in "abc")
        self.assertTrue(self.room.move_queued(c["id"], 0))
        self.assertTrue(self.room.move_queued(a["id"], 99))
        self.assertEqual(self.messages(), ["c", "b", "a"])
        self.drain()
        self.assertEqual(self.ran, ["first", "c", "b", "a"])

    def test_stop_clears_the_queue_and_its_undo(self):
        self.busy()
        kept = self.room.send("kept?", "auto")
        gone = self.room.send("gone", "auto")
        self.assertTrue(self.room.remove_queued(kept["id"]))
        self.room.cancel()
        self.assertEqual(self.room.state()["queue"], [])
        self.assertFalse(self.room.restore_queued(kept["id"]))
        self.assertFalse(self.room.edit_queued(gone["id"], "x"))
        self.drain()
        self.assertEqual(self.ran, ["first"])

    def test_every_change_is_announced_and_a_waiting_message_when_it_starts(self):
        channel = self.room.subscribe()
        self.busy()
        # a send that ran at once is not announced: the browser already showed it
        self.assertTrue(channel.empty())
        waiting = self.room.send("next", "auto")
        event = channel.get_nowait()
        self.assertEqual((event["type"], event["started"]), ("queue", None))
        self.assertEqual([q["id"] for q in event["queue"]], [waiting["id"]])
        self.room.edit_queued(waiting["id"], "next, edited")
        self.assertEqual(channel.get_nowait()["queue"][0]["message"], "next, edited")
        self.drain()
        started = channel.get(timeout=5)
        self.assertEqual(started["queue"], [])
        self.assertEqual(started["started"]["message"], "next, edited")
        self.assertEqual(self.ran, ["first", "next, edited"])

    def test_undo_after_the_queue_drained_starts_it_and_announces_it(self):
        self.busy()
        late = self.room.send("late", "auto")
        self.room.remove_queued(late["id"])
        self.drain()
        channel = self.room.subscribe()
        self.assertTrue(self.room.restore_queued(late["id"]))
        self.drain()
        events = [channel.get(timeout=5), channel.get(timeout=5)]
        self.assertEqual([e["started"]["message"] for e in events if e["started"]], ["late"])
        self.assertEqual(self.ran, ["first", "late"])

    @staticmethod
    def _all(channel):
        events = []
        while True:
            try:
                events.append(channel.get_nowait())
            except queuelib.Empty:
                return events

    def test_stop_between_dequeue_and_start_announces_no_started(self):
        # AG-10. A Stop that lands after the worker takes a queued message but
        # before its turn is announced must not emit a `started`: that would
        # relight "Working…" with no turn behind it. Timed deterministically --
        # the worker is held the instant before it announces "second", the Stop
        # is fired there, and only then is the announce let through.
        channel = self.room.subscribe()
        self.busy()                              # "first" is running, held open
        self.room.send("second", "auto")         # waits behind it
        real_announce = self.room._announce_start
        reached, proceed = threading.Event(), threading.Event()

        def gated_announce(entry):
            reached.set()
            proceed.wait(5)
            return real_announce(entry)

        self.room._announce_start = gated_announce
        self.release.set()                       # let "first" finish; worker pops "second"
        self.assertTrue(reached.wait(5), "the worker never reached the announce")
        self.room.cancel()                       # Stop lands between the pop and the emit
        proceed.set()
        worker = self.room._worker
        if worker:
            worker.join(5)

        events = self._all(channel)
        started = [e for e in events if e["type"] == "queue" and e.get("started")]
        self.assertEqual(started, [], "a cancelled entry must not announce a start")
        cancelled = [i for i, e in enumerate(events) if e["type"] == "turn_done" and e.get("cancelled")]
        self.assertTrue(cancelled, "the Stop was never announced")
        # nothing that opens a turn follows the cancelled turn_done
        after = events[cancelled[0] + 1:]
        self.assertFalse([e for e in after if e.get("started")])
        self.assertEqual(self.ran, ["first"], "the cancelled message never ran")

    def test_every_queue_snapshot_is_emitted_while_the_lock_is_held(self):
        # AG-10/AG-11 rest on a queue snapshot going out inside the same
        # critical section that took it, so a Stop or a racing edit can't slip
        # between the snapshot and its announce. A plain Lock is not reentrant,
        # so a failed non-blocking acquire from the emitting thread proves the
        # lock is held at that exact point.
        held = []
        real = self.room._emit_queue

        def spy(rev, snapshot, started=None):
            got = self.room._lock.acquire(blocking=False)
            held.append(not got)
            if got:
                self.room._lock.release()
            real(rev, snapshot, started)

        self.room._emit_queue = spy
        self.busy()
        waiting = self.room.send("second", "auto")   # a queued send announces
        self.room.edit_queued(waiting["id"], "edited")
        self.room.remove_queued(waiting["id"])
        self.room.restore_queued(waiting["id"])
        self.room.move_queued(waiting["id"], 0)
        self.drain()                                 # "second" starts: _announce_start announces
        self.room.cancel()                           # cancel announces an empty queue
        self.assertEqual(self.ran, ["first", "edited"])
        self.assertTrue(len(held) >= 6 and all(held),
                        "a queue snapshot was announced outside the lock")

    def test_concurrent_mutations_last_applied_matches_state(self):
        # AG-11. Two threads mutate the queue at once. Whatever the interleaving,
        # the copy a browser ends up showing -- the one with the highest revision
        # it saw -- matches the server's real queue.
        channel = self.room.subscribe()
        self.busy()
        ids = [self.room.send(str(i), "auto")["id"] for i in range(6)]
        barrier = threading.Barrier(2)

        def mover():
            barrier.wait()
            for entry_id in ids:
                self.room.move_queued(entry_id, 0)

        def editor():
            barrier.wait()
            for entry_id in ids:
                self.room.edit_queued(entry_id, f"{entry_id}!")

        threads = [threading.Thread(target=mover), threading.Thread(target=editor)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        applied, last_rev = None, -1
        for event in self._all(channel):
            if event["type"] == "queue" and event["rev"] > last_rev:
                last_rev, applied = event["rev"], event["queue"]
        self.assertIsNotNone(applied)
        self.assertEqual(applied, self.room.state()["queue"])


class RunTurnStopTests(unittest.TestCase):
    """The real _run_turn's two Stop windows: before launch and during launch."""

    def test_stop_before_launch_starts_no_process_and_is_silent(self):
        room = chat.ChatSession("id", "/repo", "claude")
        channel = room.subscribe()
        room._api_cancel.set()                   # a Stop already announced the end
        with mock.patch.object(chat.subprocess, "Popen") as popen:
            room._run_turn("hi", "auto")
        popen.assert_not_called()
        self.assertTrue(channel.empty(), "cancel() already emitted turn_done; no second one")

    def test_stop_during_launch_kills_the_process_and_does_not_run(self):
        # AG-10 companion: a Stop during Popen leaves cancel() with no _proc to
        # kill. _run_turn must re-check under the lock, kill the just-started
        # process, and not stream the turn to completion after "Stopped".
        room = chat.ChatSession("id", "/repo", "claude")
        channel = room.subscribe()
        proc = mock.Mock(pid=99)
        proc.poll.return_value = None
        proc.stdout = io.StringIO('{"type":"result","is_error":false}\n')

        def popen(*_a, **_k):
            room._api_cancel.set()               # Stop lands while the process starts
            return proc

        with mock.patch.object(chat.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(chat.os, "getpgid", return_value=99), \
                mock.patch.object(chat.os, "killpg") as kill:
            room._run_turn("hi", "auto")
        kill.assert_called_once()
        self.assertIsNone(room._proc, "a cancelled process is never registered")
        self.assertFalse(room._running)
        self.assertTrue(channel.empty(), "the turn streamed nothing after the Stop")


class ManagerAndRouteTests(unittest.TestCase):
    def test_manager_routes_actions_and_refuses_bad_ones(self):
        manager = chat.ChatManager()
        self.assertFalse(manager.queue_action("nobody", "remove", "q1"))
        room = manager.session("sid", "/repo")
        with mock.patch.object(room, "move_queued", return_value=True) as move:
            self.assertTrue(manager.queue_action("sid", "move", "q2", index=0))
        move.assert_called_once_with("q2", 0)
        with self.assertRaises(ValueError):
            manager.queue_action("sid", "move", "q2", index="first")
        with self.assertRaises(ValueError):
            manager.queue_action("sid", "resend", "q2")
        self.assertEqual(manager.state("unknown"), {"running": False, "queued": 0, "queue": []})

    def handler(self):
        handler = object.__new__(web.Handler)
        handler._session_by_id = mock.Mock(
            return_value=SimpleNamespace(session_id="sid", cwd="/repo", engine="claude"))
        handler._send_json = mock.Mock()
        handler.chat = mock.Mock()
        handler.chat.state.return_value = {"running": True, "queued": 1,
                                           "queue": [{"id": "q2", "message": "hi"}]}
        return handler

    def test_route_edits_and_replies_with_the_queue(self):
        handler = self.handler()
        handler.chat.queue_action.return_value = True
        handler._chat_queue({"sessionId": "sid", "action": "edit", "id": "q2", "message": "hi"})
        handler.chat.queue_action.assert_called_once_with("sid", "edit", "q2", message="hi", index=None)
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(body["queue"], [{"id": "q2", "message": "hi"}])

    def test_route_says_when_the_message_is_gone_or_the_request_is_wrong(self):
        handler = self.handler()
        handler.chat.queue_action.return_value = False
        handler._chat_queue({"sessionId": "sid", "action": "edit", "id": "q1", "message": "x"})
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, 409)
        self.assertTrue(body["gone"])
        self.assertIn("already started", body["error"])
        self.assertEqual(body["queued"], 1)
        handler._chat_queue({"sessionId": "sid", "action": "restore", "id": "q1"})
        self.assertIn("Stop cleared", handler._send_json.call_args.args[1]["error"])
        handler.chat.queue_action.side_effect = ValueError("Say where to move it.")
        handler._chat_queue({"sessionId": "sid", "action": "move", "id": "q1"})
        status, body = handler._send_json.call_args.args
        self.assertEqual((status, body["error"]), (400, "Say where to move it."))
        handler._session_by_id.return_value = None
        handler._chat_queue({"sessionId": "gone", "action": "remove", "id": "q1"})
        self.assertEqual(handler._send_json.call_args.args[0], 404)

    def test_send_reports_whether_the_message_waits(self):
        handler = self.handler()
        handler._valid_attachments = mock.Mock(return_value=[])
        handler.chat.state.return_value = {"running": False}
        handler.chat.send.return_value = {"id": "q3", "queued": True}
        handler._chat_send({"sessionId": "sid", "message": "hello"})
        self.assertEqual(handler._send_json.call_args.args,
                         (200, {"ok": True, "id": "q3", "queued": True}))

    def codex_handler(self, pid=None, status="idle", running=False):
        handler = object.__new__(web.Handler)
        handler._session_by_id = mock.Mock(
            return_value=SimpleNamespace(session_id="sid", cwd="/repo",
                                         engine="codex", pid=pid, status=status))
        handler._send_json = mock.Mock()
        handler.chat = mock.Mock()
        handler.chat.state.return_value = {"running": running, "queued": 0, "queue": []}
        handler.chat.queue_action.return_value = True
        return handler

    def test_restore_refuses_when_the_codex_thread_is_held(self):
        # AG-12. Undo (restore) can start a turn the instant the queue has
        # drained, so it takes the same Codex busy check a send does. A thread
        # held by an interactive codex (the card carries a pid) is refused with
        # 409 and restore is never attempted -- the message stays removed, no
        # "already has an active writer" crash.
        handler = self.codex_handler(pid=4321)
        handler._chat_queue({"sessionId": "sid", "action": "restore", "id": "q1"})
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, 409)
        self.assertIn("Open in Terminal", body["error"])
        handler.chat.queue_action.assert_not_called()

    def test_restore_proceeds_when_the_codex_thread_is_free(self):
        handler = self.codex_handler(pid=None, status="idle")
        handler._chat_queue({"sessionId": "sid", "action": "restore", "id": "q1"})
        handler.chat.queue_action.assert_called_once_with("sid", "restore", "q1",
                                                          message=None, index=None)
        self.assertEqual(handler._send_json.call_args.args[0], 200)

    def test_the_codex_check_guards_restore_only_not_edit(self):
        # A held thread does not block edit/remove/move -- those never start a
        # turn, so only restore needs the check.
        handler = self.codex_handler(pid=4321)
        handler._chat_queue({"sessionId": "sid", "action": "edit", "id": "q1", "message": "x"})
        handler.chat.queue_action.assert_called_once()
        self.assertEqual(handler._send_json.call_args.args[0], 200)


if __name__ == "__main__":
    unittest.main()
