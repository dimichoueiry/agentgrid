"""AG-14: durable, editable, duplicable agent system prompts.

Four surfaces, one feature:
  * the per-session standing-prompt store in discovery (durable, migration-free);
  * the chat engine folding that prompt into every FUTURE turn it drives;
  * the library duplicate that copies a whole reusable configuration;
  * the HTTP routes the UI drives all of the above through.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from agentgrid import chat, discovery, web


# ---------------------------------------------------------------------------
# The durable per-session store.


class SystemPromptStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "system_prompts.json"
        patcher = mock.patch.object(discovery, "SYSTEM_PROMPTS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_save_then_read_round_trips(self):
        kept = discovery.save_system_prompt("s1", "  Be terse.  ")
        self.assertEqual(kept, "Be terse.")  # trimmed
        self.assertEqual(discovery.system_prompt_for("s1"), "Be terse.")

    def test_empty_clears_the_entry_rather_than_storing_blank(self):
        discovery.save_system_prompt("s1", "Something")
        discovery.save_system_prompt("s1", "   ")
        self.assertEqual(discovery.system_prompt_for("s1"), "")
        # A cleared prompt leaves no key behind -- indistinguishable from unset.
        self.assertNotIn("s1", discovery.load_system_prompts())

    def test_prompt_is_capped(self):
        kept = discovery.save_system_prompt("s1", "x" * (discovery.MAX_SYSTEM_PROMPT + 500))
        self.assertEqual(len(kept), discovery.MAX_SYSTEM_PROMPT)
        self.assertEqual(len(discovery.system_prompt_for("s1")), discovery.MAX_SYSTEM_PROMPT)

    def test_unknown_and_empty_ids_read_as_empty(self):
        self.assertEqual(discovery.system_prompt_for("nope"), "")
        self.assertEqual(discovery.system_prompt_for(""), "")

    def test_sessions_do_not_bleed_into_each_other(self):
        discovery.save_system_prompt("a", "prompt A")
        discovery.save_system_prompt("b", "prompt B")
        self.assertEqual(discovery.system_prompt_for("a"), "prompt A")
        self.assertEqual(discovery.system_prompt_for("b"), "prompt B")

    def test_missing_file_is_empty_not_an_error(self):
        # First run: nothing on disk. This is the migration story -- an install
        # that predates the feature simply has no store yet.
        self.assertFalse(self.path.exists())
        self.assertEqual(discovery.load_system_prompts(), {})
        self.assertEqual(discovery.system_prompt_for("s1"), "")

    def test_corrupt_file_is_empty_not_an_error(self):
        self.path.write_text("{ this is not json", "utf-8")
        self.assertEqual(discovery.system_prompt_for("s1"), "")

    def test_writes_are_atomic_and_leave_valid_json(self):
        discovery.save_system_prompt("s1", "Be terse.")
        # os.replace means a reader never sees the temp file left behind.
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())
        self.assertEqual(json.loads(self.path.read_text("utf-8")), {"s1": "Be terse."})


# ---------------------------------------------------------------------------
# Chat-engine injection: the pure helpers, then the turn that uses them.


class InjectionHelperTests(unittest.TestCase):
    def test_append_flags_only_when_there_is_a_prompt(self):
        self.assertEqual(chat.append_system_prompt_flags(""), [])
        self.assertEqual(chat.append_system_prompt_flags("   "), [])
        self.assertEqual(chat.append_system_prompt_flags("Be terse."),
                         ["--append-system-prompt", "Be terse."])

    def test_framing_wraps_only_when_there_is_a_prompt(self):
        self.assertEqual(chat.frame_system_prompt("do it", ""), "do it")
        framed = chat.frame_system_prompt("do it", "Be terse.")
        self.assertIn("<system instructions>\nBe terse.\n</system instructions>", framed)
        self.assertTrue(framed.endswith("do it"))


class FakeProc:
    """A launched CLI that produces no output and exits cleanly."""

    def __init__(self, argv):
        self.argv = argv
        self.stdout = iter(())
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class TurnInjectionTests(unittest.TestCase):
    """The stored prompt reaches the actual argv of the turn AgentGrid drives."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        path = Path(self._tmp.name) / "system_prompts.json"
        p = mock.patch.object(discovery, "SYSTEM_PROMPTS_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        cx = mock.patch.object(chat, "codex_binary", lambda: "codex")
        cx.start()
        self.addCleanup(cx.stop)

    def _argv_of_turn(self, session_id, engine):
        captured = {}

        def fake_popen(argv, *a, **k):
            captured["argv"] = argv
            return FakeProc(argv)

        session = chat.ChatSession(session_id, "/tmp", engine)
        with mock.patch.object(chat.subprocess, "Popen", fake_popen):
            session._run_turn("do the thing", "auto")
        return captured["argv"]

    def test_claude_turn_appends_the_stored_prompt_as_a_flag(self):
        discovery.save_system_prompt("sess", "You are a security reviewer.")
        argv = self._argv_of_turn("sess", "claude")
        self.assertIn("--append-system-prompt", argv)
        i = argv.index("--append-system-prompt")
        self.assertEqual(argv[i + 1], "You are a security reviewer.")

    def test_claude_turn_without_a_stored_prompt_adds_no_flag(self):
        argv = self._argv_of_turn("sess", "claude")
        self.assertNotIn("--append-system-prompt", argv)

    def test_codex_turn_frames_the_stored_prompt_into_the_message(self):
        discovery.save_system_prompt("cx", "Only touch tests.")
        argv = self._argv_of_turn("cx", "codex")
        # The message is the last argv element on the codex path.
        self.assertIn("<system instructions>\nOnly touch tests.\n</system instructions>",
                      argv[-1])
        self.assertTrue(argv[-1].endswith("do the thing"))

    def test_codex_turn_without_a_stored_prompt_is_left_alone(self):
        argv = self._argv_of_turn("cx", "codex")
        self.assertEqual(argv[-1], "do the thing")

    def test_a_new_chat_with_no_id_yet_carries_nothing_extra(self):
        # A brand-new chat mints its id mid-turn; there is nothing to have set a
        # prompt against, so the first turn must run clean.
        argv = self._argv_of_turn(None, "claude")
        self.assertNotIn("--append-system-prompt", argv)


# ---------------------------------------------------------------------------
# The library duplicate (pure functions).


class LibraryDuplicateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        path = Path(self._tmp.name) / "agents.json"
        p = mock.patch.object(web, "AGENTS_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_unique_name_suffixes_and_caps(self):
        self.assertEqual(web._unique_agent_name("Builder", set()), "Builder")
        self.assertEqual(web._unique_agent_name("Builder", {"builder"}), "Builder (copy)")
        self.assertEqual(web._unique_agent_name("Builder", {"builder", "builder (copy)"}),
                         "Builder (copy 2)")
        long = "N" * 100
        self.assertLessEqual(len(web._unique_agent_name(long, {long[:60].lower()})), 60)

    def test_add_never_overwrites_an_existing_definition(self):
        web.save_saved_agent("Builder", "claude", "opus", "original")
        agents, name = web.add_saved_agent("Builder", "codex", "gpt", "second")
        self.assertEqual(name, "Builder (copy)")
        self.assertEqual(len(agents), 2)
        self.assertEqual(agents[0]["systemPrompt"], "original")  # untouched

    def test_duplicate_copies_the_whole_reusable_configuration(self):
        web.save_saved_agent("Reviewer", "codex", "gpt-5", "Look for bugs." * 20)
        agents, name = web.duplicate_saved_agent("reviewer")  # case-insensitive
        self.assertEqual(name, "Reviewer (copy)")
        copy = next(a for a in agents if a["name"] == name)
        original = next(a for a in agents if a["name"] == "Reviewer")
        self.assertEqual(copy["engine"], original["engine"])
        self.assertEqual(copy["model"], original["model"])
        self.assertEqual(copy["systemPrompt"], original["systemPrompt"])

    def test_duplicating_an_unknown_agent_changes_nothing(self):
        web.save_saved_agent("Only", "claude", "", "")
        agents, name = web.duplicate_saved_agent("ghost")
        self.assertIsNone(name)
        self.assertEqual([a["name"] for a in agents], ["Only"])


# ---------------------------------------------------------------------------
# The HTTP routes, end to end.


def _session(session_id, *, engine="claude", model="opus", name="Nice Name"):
    return discovery.Session(
        session_id=session_id, kind="background", status="idle", cwd="/tmp",
        started_at=0, engine=engine, model=model, custom_name=name)


def _serve(sessions):
    fleet = SimpleNamespace(raw=lambda: sessions)
    bound = type("BoundHandler", (web.Handler,),
                 {"fleet": fleet, "token": "tok", "chat": None,
                  "log_message": lambda *a, **k: None})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _call(port, method, route, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}{'&' if '?' in route else '?'}t=tok",
        data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        base = Path(self._tmp.name)
        for module, attr, name in ((discovery, "SYSTEM_PROMPTS_PATH", "system_prompts.json"),
                                   (web, "AGENTS_PATH", "agents.json")):
            p = mock.patch.object(module, attr, base / name)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_view_is_empty_before_anything_is_set(self):
        port = _serve([_session("s1")]).server_address[1]
        status, body = _call(port, "GET", "/api/system-prompt?session=s1")
        self.assertEqual(status, 200)
        self.assertEqual(body["systemPrompt"], "")
        self.assertEqual(body["maxLength"], discovery.MAX_SYSTEM_PROMPT)

    def test_set_then_view_round_trips_through_the_store(self):
        port = _serve([_session("s1")]).server_address[1]
        status, body = _call(port, "POST", "/api/system-prompt",
                             {"sessionId": "s1", "systemPrompt": "  Be terse.  "})
        self.assertEqual(status, 200)
        self.assertEqual(body["systemPrompt"], "Be terse.")  # trimmed, echoed
        self.assertEqual(discovery.system_prompt_for("s1"), "Be terse.")
        _, view = _call(port, "GET", "/api/system-prompt?session=s1")
        self.assertEqual(view["systemPrompt"], "Be terse.")

    def test_set_requires_a_session_id(self):
        port = _serve([]).server_address[1]
        status, body = _call(port, "POST", "/api/system-prompt", {"systemPrompt": "x"})
        self.assertEqual(status, 400)
        self.assertIn("session id", body["error"])

    def test_duplicate_a_library_definition(self):
        web.save_saved_agent("Reviewer", "codex", "gpt-5", "Look for bugs.")
        port = _serve([]).server_address[1]
        status, body = _call(port, "POST", "/api/agents/duplicate", {"name": "Reviewer"})
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "Reviewer (copy)")
        self.assertEqual(len(body["agents"]), 2)

    def test_duplicate_unknown_library_definition_is_404(self):
        port = _serve([]).server_address[1]
        status, body = _call(port, "POST", "/api/agents/duplicate", {"name": "ghost"})
        self.assertEqual(status, 404)

    def test_capture_a_running_session_into_a_new_definition(self):
        discovery.save_system_prompt("s1", "Standing orders.")
        port = _serve([_session("s1", engine="codex", model="gpt-5",
                                name="My Live Agent")]).server_address[1]
        status, body = _call(port, "POST", "/api/agents/duplicate", {"sessionId": "s1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "My Live Agent")
        saved = next(a for a in body["agents"] if a["name"] == "My Live Agent")
        self.assertEqual(saved["engine"], "codex")
        self.assertEqual(saved["model"], "gpt-5")
        self.assertEqual(saved["systemPrompt"], "Standing orders.")

    def test_capture_unknown_session_is_404(self):
        port = _serve([]).server_address[1]
        status, _ = _call(port, "POST", "/api/agents/duplicate", {"sessionId": "gone"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
