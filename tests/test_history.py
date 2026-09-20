"""Chat history: every conversation on disk stays reachable, and deletable.

The bug these pin: the board only ever showed what was running plus a 24-hour
window of ended sessions, so a conversation became unreachable the day after
it ended even though its transcript was still on disk and `--resume` would
still have worked. And the transcript reader stopped dead at 600 turns, so the
start of a long conversation could not be read at all.

Nothing here may touch the real home directory: every store is redirected to a
temporary one first.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import discovery, history, web


def turn(index: int, cwd: str, session_id: str) -> list[dict]:
    """One exchange, in the shape Claude Code really writes."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_700_000_000 + index))
    common = {"cwd": cwd, "gitBranch": "main", "sessionId": session_id,
              "timestamp": stamp, "isSidechain": False, "userType": "external",
              "version": "1.0.0", "parentUuid": None}
    return [
        {**common, "type": "user", "uuid": f"u{index}",
         "message": {"role": "user", "content": f"question {index}"}},
        {**common, "type": "assistant", "uuid": f"a{index}",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": f"answer {index}"}]}},
    ]


class HistoryCase(unittest.TestCase):
    """Isolates the transcript store, the trash and every sidecar file."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.projects = self.home / "projects"
        self.projects.mkdir()
        self._patch(discovery, "PROJECTS_DIR", self.projects)
        self._patch(history, "TRASH_DIR", self.home / "trash")
        self.sidecars = {name: self.home / f"{name}.json" for name in history.SIDECAR_PATHS}
        self._patch(history, "SIDECAR_PATHS", self.sidecars)
        # The module caches are keyed by path and would otherwise carry state
        # between tests -- and, worse, from the real store into a test.
        self._patch(history, "_CACHE", discovery.TranscriptCache())
        self._patch(history, "_TURNS", {})
        self._patch(history, "_CWD", {})

    def _patch(self, module, attribute, value):
        patcher = mock.patch.object(module, attribute, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, session_id: str, cwd: str, turns: int = 2, *,
              title: str = "", last_prompt: str = "", when: float | None = None) -> Path:
        """Put a transcript in the store the way Claude Code lays them out."""
        folder = self.projects / discovery.project_slug(cwd)
        folder.mkdir(parents=True, exist_ok=True)
        entries: list[dict] = []
        if title:
            entries.append({"type": "ai-title", "aiTitle": title, "sessionId": session_id})
        for index in range(turns):
            entries.extend(turn(index, cwd, session_id))
        if last_prompt:
            entries.append({"type": "last-prompt", "lastPrompt": last_prompt,
                            "leafUuid": "x", "sessionId": session_id})
        path = folder / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        if when is not None:
            import os
            os.utime(path, (when, when))
        return path

    def sidecar(self, name: str, payload: dict) -> None:
        self.sidecars[name].write_text(json.dumps(payload), encoding="utf-8")

    def read_sidecar(self, name: str) -> dict:
        try:
            return json.loads(self.sidecars[name].read_text("utf-8"))
        except (OSError, ValueError):
            return {}


# --- reading the store --------------------------------------------------------

class ScanTests(HistoryCase):
    def test_every_transcript_is_listed_however_old_it_is(self):
        """The bug: a card vanished after 24 hours and nothing could reach it."""
        now = time.time()
        self.write("aaa", "/repo/one", when=now)
        self.write("bbb", "/repo/two", when=now - 40 * 24 * 3600)   # 40 days old
        found = history.scan()
        self.assertEqual([c.session_id for c in found], ["aaa", "bbb"], "newest first")
        self.assertGreater(now - found[1].modified, discovery.ENDED_INTERACTIVE_WINDOW,
                           "the old one is far outside the board's window and still listed")

    def test_a_row_carries_what_it_takes_to_recognise_a_conversation(self):
        self.write("aaa", "/repo/one", turns=3, title="Fixing the parser",
                   last_prompt="try the other branch")
        row = history.page(0, 10)["conversations"][0]
        self.assertEqual(row["title"], "Fixing the parser")
        self.assertEqual(row["preview"], "try the other branch")
        self.assertEqual(row["cwd"], "/repo/one")
        self.assertEqual(row["project"], "one")
        self.assertEqual(row["turns"], 8, "3 exchanges, a title line and a last-prompt line")
        self.assertGreater(row["size"], 0, "size is shown so history doubles as cleanup")

    def test_a_page_is_a_window_over_the_whole_store(self):
        for index in range(12):
            self.write(f"s{index:02d}", "/repo/one", when=time.time() - index)
        first = history.page(0, 5)
        self.assertEqual(first["total"], 12)
        self.assertEqual([c["sessionId"] for c in first["conversations"]],
                         ["s00", "s01", "s02", "s03", "s04"])
        self.assertEqual([c["sessionId"] for c in history.page(10, 5)["conversations"]],
                         ["s10", "s11"])

    def test_search_matches_a_title_a_preview_or_a_path(self):
        self.write("aaa", "/repo/one", title="Fixing the parser", last_prompt="ship it")
        self.write("bbb", "/repo/two", title="Billing rewrite", last_prompt="ship it")
        self.assertEqual([c["sessionId"] for c in history.page(0, 10, search="parser")["conversations"]],
                         ["aaa"])
        self.assertEqual({c["sessionId"] for c in history.page(0, 10, search="ship it")["conversations"]},
                         {"aaa", "bbb"})
        self.assertEqual([c["sessionId"] for c in history.page(0, 10, search="two")["conversations"]],
                         ["bbb"])

    def test_the_project_filter_groups_on_the_recorded_cwd(self):
        self.write("aaa", "/repo/one")
        self.write("bbb", "/repo/one")
        self.write("ccc", "/repo/two")
        history.warm()
        self.assertEqual([(p["name"], p["count"]) for p in history.projects()],
                         [("one", 2), ("two", 1)])
        self.assertEqual([c["sessionId"] for c in history.page(0, 10, project="two")["conversations"]],
                         ["ccc"])

    def test_an_id_from_a_client_never_becomes_a_path(self):
        self.write("aaa", "/repo/one")
        for hostile in ("../../etc/passwd", "..", "a/b", ""):
            self.assertEqual(history.safe_id(hostile), "")
            self.assertIsNone(history.find(hostile))
        self.assertIsNotNone(history.find("aaa"))
        self.assertIsNone(history.find("never-existed"))


# --- deleting, reversibly -----------------------------------------------------

class DeleteTests(HistoryCase):
    def test_delete_takes_the_transcript_and_every_trace_of_it(self):
        path = self.write("aaa", "/repo/one", title="Fixing the parser")
        self.sidecar("names", {"aaa": "My chat", "other": "keep me"})
        self.sidecar("tags", {"aaa": ["x"]})
        self.sidecar("read", {"aaa": 1234.0})
        history.delete("aaa")
        self.assertFalse(path.exists(), "the transcript is out of the store")
        self.assertEqual(history.scan(), [])
        self.assertEqual(self.read_sidecar("names"), {"other": "keep me"},
                         "only this conversation's entry goes")
        self.assertEqual(self.read_sidecar("tags"), {})
        self.assertEqual(self.read_sidecar("read"), {})

    def test_delete_is_reversible_until_it_is_purged(self):
        path = self.write("aaa", "/repo/one", title="Fixing the parser")
        original = path.read_text("utf-8")
        self.sidecar("names", {"aaa": "My chat"})
        self.sidecar("read", {"aaa": 1234.0})
        history.delete("aaa")
        self.assertEqual([t["sessionId"] for t in history.trashed()], ["aaa"])
        self.assertTrue(history.restore("aaa"))
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text("utf-8"), original, "byte for byte")
        self.assertEqual(self.read_sidecar("names"), {"aaa": "My chat"}, "and what it had")
        self.assertEqual(self.read_sidecar("read"), {"aaa": 1234.0})
        self.assertEqual(history.trashed(), [])
        self.assertEqual([c.session_id for c in history.scan()], ["aaa"])

    def test_restoring_something_that_is_not_in_the_trash_says_so(self):
        self.assertFalse(history.restore("aaa"))
        self.assertFalse(history.restore("../escape"))

    def test_the_trash_empties_itself_only_once_the_window_has_passed(self):
        self.write("aaa", "/repo/one")
        self.write("bbb", "/repo/one")
        history.delete("aaa")
        history.delete("bbb")
        # bbb was deleted a fortnight ago as far as its manifest is concerned
        home = history.TRASH_DIR / "bbb"
        manifest = json.loads((home / "manifest.json").read_text("utf-8"))
        manifest["deletedAt"] = time.time() - history.TRASH_TTL - 60
        (home / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(history.purge_expired(), ["bbb"])
        self.assertEqual([t["sessionId"] for t in history.trashed()], ["aaa"],
                         "the recent one is still recoverable")
        self.assertFalse(history.restore("bbb"))

    def test_a_conversation_can_be_emptied_from_the_trash_at_once(self):
        self.write("aaa", "/repo/one")
        history.delete("aaa")
        self.assertTrue(history.purge_now("aaa"))
        self.assertEqual(history.trashed(), [])
        self.assertFalse(history.purge_now("aaa"), "a second time changes nothing")

    def test_deleting_what_is_not_there_is_refused_not_guessed_at(self):
        with self.assertRaises(ValueError):
            history.delete("never-existed")
        with self.assertRaises(ValueError):
            history.delete("../../etc/passwd")


# --- the HTTP surface ---------------------------------------------------------

class HistoryRouteTests(HistoryCase):
    def handler(self, *, live: set[str] | None = None, running: set[str] | None = None):
        handler = object.__new__(web.Handler)
        sessions = [SimpleNamespace(session_id=sid, status="working") for sid in (live or set())]
        handler.fleet = SimpleNamespace(raw=lambda: sessions)
        handler.chat = SimpleNamespace(
            state=lambda sid: {"running": sid in (running or set()), "queued": 0, "queue": []})
        self.replies = []
        handler._send_json = lambda code, data: self.replies.append((code, data))
        return handler

    def last(self):
        return self.replies[-1]

    def test_the_listing_says_which_conversations_are_live(self):
        self.write("aaa", "/repo/one", title="Running one")
        self.write("bbb", "/repo/one", title="Finished one")
        self.handler(live={"aaa"})._get_conversations({})
        code, payload = self.last()
        self.assertEqual(code, 200)
        live = {c["sessionId"]: c["live"] for c in payload["conversations"]}
        self.assertEqual(live, {"aaa": True, "bbb": False})
        self.assertEqual(payload["total"], 2)
        self.assertTrue(payload["projects"])

    def test_a_running_conversation_cannot_be_deleted(self):
        path = self.write("aaa", "/repo/one")
        handler = self.handler(live={"aaa"})
        handler._conversation_action("delete", {"sessionId": "aaa"})
        self.assertEqual(self.last()[0], 400)
        self.assertIn("still running", self.last()[1]["error"])
        self.assertTrue(path.exists(), "nothing was taken out from under it")

    def test_a_turn_this_server_is_driving_also_counts_as_running(self):
        self.write("aaa", "/repo/one")
        handler = self.handler(running={"aaa"})
        handler._conversation_action("delete", {"sessionId": "aaa"})
        self.assertEqual(self.last()[0], 400)

    def test_delete_and_undo_over_the_route(self):
        path = self.write("aaa", "/repo/one")
        handler = self.handler()
        handler._conversation_action("delete", {"sessionId": "aaa"})
        self.assertEqual(self.last()[0], 200)
        self.assertFalse(path.exists())
        handler._conversation_action("restore", {"sessionId": "aaa"})
        self.assertEqual(self.last()[0], 200)
        self.assertTrue(path.exists())

    def test_any_conversation_on_disk_can_be_read_back(self):
        self.write("aaa", "/repo/one", turns=3, title="Old one",
                   when=time.time() - 40 * 24 * 3600)
        handler = self.handler()
        handler._get_conversation_transcript({"id": ["aaa"]})
        code, payload = self.last()
        self.assertEqual(code, 200)
        self.assertTrue(payload["blocks"], "40 days old and still readable")
        handler._get_conversation_transcript({"id": ["../../etc/passwd"]})
        self.assertEqual(self.last()[0], 404)


# --- the 600-turn wall --------------------------------------------------------

class TranscriptWindowTests(unittest.TestCase):
    """`blocks[-600:]` meant the start of a long conversation was unreachable."""

    @staticmethod
    def blocks(count: int) -> list[dict]:
        return [{"kind": "user", "text": f"line {i}"} for i in range(count)]

    def test_the_default_read_is_the_tail_and_says_more_is_behind_it(self):
        window = web.Handler._window(self.blocks(1500), {})
        self.assertEqual(len(window["blocks"]), web.TRANSCRIPT_WINDOW)
        self.assertEqual(window["total"], 1500)
        self.assertEqual(window["offset"], 900)
        self.assertTrue(window["hasOlder"])
        self.assertEqual(window["blocks"][-1]["text"], "line 1499", "newest last")

    def test_older_turns_are_reachable_a_window_at_a_time(self):
        blocks = self.blocks(1500)
        window = web.Handler._window(blocks, {"before": ["900"]})
        self.assertEqual(window["offset"], 300)
        self.assertEqual(window["blocks"][0]["text"], "line 300")
        self.assertEqual(window["blocks"][-1]["text"], "line 899", "joins the tail exactly")
        first = web.Handler._window(blocks, {"before": ["300"]})
        self.assertEqual(first["offset"], 0)
        self.assertEqual(first["blocks"][0]["text"], "line 0", "the first line is reachable")
        self.assertFalse(first["hasOlder"])

    def test_a_short_conversation_arrives_whole(self):
        window = web.Handler._window(self.blocks(12), {})
        self.assertEqual(len(window["blocks"]), 12)
        self.assertFalse(window["hasOlder"])

    def test_a_nonsense_window_falls_back_instead_of_failing(self):
        blocks = self.blocks(50)
        for query in ({"before": ["nope"]}, {"limit": ["nope"]}, {"before": ["-5"]},
                      {"before": ["999999"]}, {"limit": ["0"]}):
            window = web.Handler._window(blocks, query)
            self.assertLessEqual(len(window["blocks"]), 50)
            self.assertEqual(window["total"], 50)

    def test_a_client_cannot_ask_for_an_unbounded_read(self):
        window = web.Handler._window(self.blocks(9000), {"limit": ["100000"]})
        self.assertEqual(len(window["blocks"]), web.TRANSCRIPT_WINDOW_MAX)


if __name__ == "__main__":
    unittest.main()
