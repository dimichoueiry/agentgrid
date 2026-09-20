"""A queued message outlives the server: it is kept on disk, and never dropped silently.

The queue is the one piece of chat state only the server holds -- the CLI has
not seen a waiting message and the browser stopped showing it the moment it was
queued. So it is written through to disk, read back at startup, and a turn that
never reached the CLI hands its message back instead of dropping it.
"""
import io
import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import chat, web
from support import isolate_chat_queue


class QueueOnDiskTests(unittest.TestCase):
    def setUp(self):
        self.dir = isolate_chat_queue(self)
        prompts = mock.patch.object(chat.discovery, "system_prompt_for", return_value="")
        prompts.start()
        self.addCleanup(prompts.stop)
        self.ran = []
        self.holding = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def room(self, session_id="sid", engine="claude"):
        room = chat.ChatSession(session_id, "/repo", engine)
        room._run_turn = self.fake_turn
        return room

    def fake_turn(self, message, posture, model="", attachments=None):
        self.ran.append(message)
        self.holding.set()
        self.release.wait(5)
        return ""

    def busy(self, room):
        room.send("first", "auto")
        self.assertTrue(self.holding.wait(5), "the first turn never started")

    def saved(self, session_id="sid"):
        path = chat.queue_path(session_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def settle(self, room):
        for _ in range(100):
            if not (room._worker and room._worker.is_alive()):
                return
            time.sleep(0.01)

    # -- kept across a restart ------------------------------------------------

    def test_a_message_still_waiting_when_the_server_stops_comes_back(self):
        room = self.room()
        self.busy(room)
        room.send("also update the changelog", "read-only", "opus")
        self.assertEqual([q["message"] for q in room.state()["queue"]],
                         ["also update the changelog"])
        self.assertEqual(self.saved()["entries"][0]["message"], "also update the changelog")

        # The process dies here; a new run reads the queue back.
        manager = chat.ChatManager()
        self.assertEqual(manager.restore(), 1)
        waiting = manager.state("sid")["queue"]
        self.assertEqual([(q["message"], q["held"]) for q in waiting],
                         [("also update the changelog", chat.HELD_RESTART)])
        # What it needs to run again came back with it.
        entry = manager.session("sid", "")._pending[0]
        self.assertEqual((entry["posture"], entry["model"]), ("read-only", "opus"))
        self.assertEqual(manager.session("sid", "").cwd, "/repo")

    def test_a_restored_message_waits_for_the_session_rather_than_starting_itself(self):
        room = self.room()
        self.busy(room)
        room.send("later", "auto")
        manager = chat.ChatManager()
        manager.restore()
        restored = manager.session("sid", "")
        restored._run_turn = self.fake_turn

        # A restart cannot know the last run's turn is over -- the CLI may still
        # be finishing it as an orphan -- so nothing fires on startup.
        self.settle(restored)
        self.assertEqual(restored.state()["queue"][0]["held"], chat.HELD_RESTART)

        # The fleet reports the session free: now it goes.
        self.assertTrue(manager.resume_ready("sid"))
        for _ in range(100):
            if "later" in self.ran:
                break
            time.sleep(0.01)
        self.assertIn("later", self.ran)
        self.settle(restored)
        self.assertEqual(restored.state()["queue"], [])
        self.assertIsNone(self.saved(), "an empty queue leaves no file behind")

    def test_send_now_runs_a_held_message_whatever_the_fleet_says(self):
        room = self.room()
        self.busy(room)
        waiting = room.send("do it anyway", "auto")
        manager = chat.ChatManager()
        manager.restore()
        restored = manager.session("sid", "")
        restored._run_turn = self.fake_turn
        self.release.set()

        self.assertTrue(manager.queue_action("sid", "release", waiting["id"]))
        for _ in range(100):
            if "do it anyway" in self.ran:
                break
            time.sleep(0.01)
        self.assertIn("do it anyway", self.ran)
        self.assertFalse(manager.queue_action("sid", "release", "nope"))

    def test_every_change_to_the_queue_reaches_the_file(self):
        room = self.room()
        self.busy(room)
        first = room.send("one", "auto")
        second = room.send("two", "auto")
        self.assertEqual([e["message"] for e in self.saved()["entries"]], ["one", "two"])
        room.edit_queued(first["id"], "one, edited")
        self.assertEqual(self.saved()["entries"][0]["message"], "one, edited")
        room.move_queued(second["id"], 0)
        self.assertEqual([e["message"] for e in self.saved()["entries"]], ["two", "one, edited"])
        room.remove_queued(second["id"])
        self.assertEqual([e["message"] for e in self.saved()["entries"]], ["one, edited"])
        room.restore_queued(second["id"])
        self.assertEqual([e["message"] for e in self.saved()["entries"]], ["two", "one, edited"])
        room.cancel()
        self.assertIsNone(self.saved(), "Stop clears the queue on disk too")

    def test_a_queue_that_cannot_be_written_still_runs(self):
        # The file is a courtesy; the queue in memory is what sends.
        room = self.room()
        self.busy(room)
        with mock.patch.object(chat.Path, "mkdir", side_effect=OSError("read-only disk")):
            waiting = room.send("still queued", "auto")
        self.assertEqual([q["message"] for q in room.state()["queue"]], ["still queued"])
        self.release.set()
        self.settle(room)
        self.assertEqual(self.ran, ["first", "still queued"])
        self.assertIsNotNone(waiting["id"])

    def test_a_damaged_or_empty_file_is_dropped_rather_than_read(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "torn.json").write_text("{not json", encoding="utf-8")
        (self.dir / "empty.json").write_text(json.dumps(
            {"sessionId": "gone", "entries": []}), encoding="utf-8")
        manager = chat.ChatManager()
        self.assertEqual(manager.restore(), 0)
        self.assertFalse((self.dir / "empty.json").exists())

    # -- a turn that never reached the CLI ------------------------------------

    def test_a_message_whose_cli_never_started_goes_back_in_the_queue(self):
        room = chat.ChatSession("sid", "/repo", "claude")
        channel = room.subscribe()
        with mock.patch.object(chat.subprocess, "Popen",
                               side_effect=OSError("no such file")) as popen:
            room.send("the words I typed", "auto")
            self.settle(room)
        popen.assert_called_once()          # held, not retried in a loop
        waiting = room.state()["queue"]
        self.assertEqual([q["message"] for q in waiting], ["the words I typed"])
        self.assertIn("could not start claude", waiting[0]["held"])
        self.assertEqual(self.saved()["entries"][0]["message"], "the words I typed")
        kinds = [event["type"] for event in self.drain(channel)]
        self.assertIn("error", kinds)       # and the reason was said at the time
        self.assertEqual(kinds[-1], "queue")

    def test_a_message_the_model_already_received_is_not_queued_again(self):
        # The CLI opened the turn and then died. The agent has the message, so
        # putting it back would say it twice.
        room = chat.ChatSession("sid", "/repo", "claude")
        init = json.dumps({"type": "system", "subtype": "init", "session_id": "sid"})
        proc = mock.Mock(stdout=io.StringIO(init), returncode=1)
        proc.poll.return_value = 1
        with mock.patch.object(chat.subprocess, "Popen", return_value=proc):
            room.send("already delivered", "auto")
            self.settle(room)
        self.assertEqual(room.state()["queue"], [])
        self.assertIsNone(self.saved())

    def test_a_stop_during_a_failed_launch_drops_it_rather_than_holding_it(self):
        room = chat.ChatSession("sid", "/repo", "claude")

        def stopped_launch(*args, **kwargs):
            room.cancel()                   # Stop lands while the CLI is starting
            raise OSError("no such file")

        with mock.patch.object(chat.subprocess, "Popen", side_effect=stopped_launch):
            room.send("never mind", "auto")
            self.settle(room)
        self.assertEqual(room.state()["queue"], [], "Stop means dropped, not held")
        self.assertIsNone(self.saved())

    def drain(self, channel):
        events = []
        while not channel.empty():
            events.append(channel.get_nowait())
        return events


class ResumeRouteTests(unittest.TestCase):
    """What the server does with the fleet's poll, and with a Send now."""

    def sessions(self, **fields):
        base = {"session_id": "sid", "status": "done", "engine": "claude", "pid": None}
        base.update(fields)
        return [SimpleNamespace(**base)]

    def test_only_a_session_that_is_free_gets_its_held_messages(self):
        manager = mock.Mock()
        manager.state.return_value = {"running": False}
        # Mid-turn: a message now would be answered by a copy of the agent.
        web._resume_chat_queues(self.sessions(status="working"), manager)
        manager.resume_ready.assert_not_called()
        # A Codex thread a terminal holds: the CLI would refuse it.
        web._resume_chat_queues(self.sessions(engine="codex", pid=41614), manager)
        manager.resume_ready.assert_not_called()
        web._resume_chat_queues(self.sessions(), manager)
        manager.resume_ready.assert_called_once_with("sid")

    def handler(self, engine="claude", pid=None):
        handler = object.__new__(web.Handler)
        handler._session_by_id = mock.Mock(return_value=SimpleNamespace(
            session_id="sid", cwd="/repo", engine=engine, status="done", pid=pid))
        handler._send_json = mock.Mock()
        handler.chat = mock.Mock()
        handler.chat.state.return_value = {"running": False, "queued": 1, "queue": []}
        return handler

    def test_send_now_takes_the_same_codex_check_a_send_takes(self):
        handler = self.handler(engine="codex", pid=41614)
        handler._chat_queue({"sessionId": "sid", "action": "release", "id": "q1"})
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, 409)
        self.assertTrue(body["held"])
        self.assertIn("open in a terminal", body["error"])
        handler.chat.queue_action.assert_not_called()

        # With the terminal closed it goes through.
        handler = self.handler(engine="codex")
        handler.chat.queue_action.return_value = True
        handler._chat_queue({"sessionId": "sid", "action": "release", "id": "q1"})
        handler.chat.queue_action.assert_called_once_with("sid", "release", "q1",
                                                          message=None, index=None)
        self.assertEqual(handler._send_json.call_args.args[0], 200)


if __name__ == "__main__":
    unittest.main()
