"""Interactive Codex: the spawn now routes to a Terminal session for codex too,
the same way it does for claude, instead of refusing.
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

from agentgrid import web

ALLOWED = [{"path": "/tmp/proj"}]


class InteractiveRoutingTests(unittest.TestCase):
    def test_interactive_codex_no_longer_refused(self):
        captured = {}

        def fake_interactive(cwd, prompt, model, engine="claude", agent_name=""):
            captured.update(cwd=cwd, prompt=prompt, model=model, engine=engine)
            return True, "Opened an interactive codex in proj.", None

        with mock.patch.object(web, "spawn_interactive", fake_interactive):
            ok, msg, job = web.spawn_agent(
                "/tmp/proj", "do it", None, ALLOWED, engine="codex", interactive=True)

        self.assertTrue(ok)
        self.assertIsNone(job)
        self.assertEqual(captured["engine"], "codex")
        self.assertNotIn("only available for Claude", msg)

    def test_interactive_claude_still_routes_as_claude(self):
        captured = {}

        def fake_interactive(cwd, prompt, model, engine="claude", agent_name=""):
            captured["engine"] = engine
            return True, "Opened an interactive claude in proj.", None

        with mock.patch.object(web, "spawn_interactive", fake_interactive):
            web.spawn_agent("/tmp/proj", "do it", None, ALLOWED,
                            engine="claude", interactive=True)

        self.assertEqual(captured["engine"], "claude")


@unittest.skipUnless(sys.platform == "darwin", "interactive spawn uses Terminal.app")
class CodexTerminalCommandTests(unittest.TestCase):
    def test_codex_command_is_built_with_prompt_and_cwd(self):
        cap = {}

        def fake_tab(command, title):
            cap["command"] = command
            return True, "opened"

        with mock.patch.object(web, "_open_terminal_tab", fake_tab), \
             mock.patch.object(web.chat, "codex_binary", lambda: "/opt/codex"):
            ok, msg, job = web.spawn_interactive(
                "/tmp/proj", "make it pop", "gpt-5", engine="codex")

        self.assertTrue(ok)
        self.assertIsNone(job)
        self.assertIn("/opt/codex", cap["command"])
        self.assertIn("make it pop", cap["command"])
        self.assertIn("/tmp/proj", cap["command"])
        self.assertIn("codex", msg)


if __name__ == "__main__":
    unittest.main()
