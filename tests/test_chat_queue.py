"""The chat queue: messages sent mid-turn wait, visible and editable, until they start."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from agentgrid import chat, web


class QueueTests(unittest.TestCase):
    def setUp(self):
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
            "files": ["20260918-101010-abc123-shot.png"]}])

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


if __name__ == "__main__":
    unittest.main()
