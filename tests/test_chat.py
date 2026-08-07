"""Normalizer tests for the chat backend.

The fixtures are the actual `stream-json` shapes the `claude` CLI (2.1.x) emits,
captured from a real headless run: a `system/init`, `assistant` messages whose
content is a list of text/thinking/tool_use blocks, a `user` message carrying a
tool_result, and a final `result`. If the CLI's vocabulary drifts, these fail
here rather than in the browser.
"""

from __future__ import annotations

import unittest

from agentgrid import chat


class NormalizeTests(unittest.TestCase):
    def test_init_opens_the_turn_with_auth_source(self):
        raw = {"type": "system", "subtype": "init", "session_id": "s1",
               "cwd": "/repo", "model": "claude-x", "apiKeySource": "subscription"}
        self.assertEqual(chat.normalize(raw), [{
            "type": "turn_started", "sessionId": "s1", "model": "claude-x",
            "cwd": "/repo", "authSource": "subscription",
        }])

    def test_assistant_text_becomes_a_message(self):
        raw = {"type": "assistant",
               "message": {"role": "assistant",
                           "content": [{"type": "text", "text": "hello there"}]}}
        self.assertEqual(chat.normalize(raw),
                         [{"type": "assistant_message", "text": "hello there"}])

    def test_empty_thinking_is_dropped_real_thinking_is_kept(self):
        empty = {"type": "assistant",
                 "message": {"content": [{"type": "thinking", "thinking": ""}]}}
        self.assertEqual(chat.normalize(empty), [])
        real = {"type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "pondering"}]}}
        self.assertEqual(chat.normalize(real),
                         [{"type": "thinking", "text": "pondering"}])

    def test_tool_use_carries_name_and_input(self):
        raw = {"type": "assistant",
               "message": {"content": [{"type": "tool_use", "id": "toolu_1",
                                        "name": "Read", "input": {"file_path": "note.txt"}}]}}
        self.assertEqual(chat.normalize(raw), [{
            "type": "tool_use", "id": "toolu_1", "name": "Read",
            "input": {"file_path": "note.txt"},
        }])

    def test_several_blocks_in_one_message_expand_in_order(self):
        raw = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "let me look"},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}},
        ]}}
        got = [e["type"] for e in chat.normalize(raw)]
        self.assertEqual(got, ["assistant_message", "tool_use"])

    def test_tool_result_is_summarized(self):
        raw = {"type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_1", "type": "tool_result",
             "content": "1\thello from the probe file\n2\t"},
        ]}}
        events = chat.normalize(raw)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "tool_result")
        self.assertEqual(events[0]["id"], "toolu_1")
        self.assertTrue(events[0]["ok"])
        self.assertIn("hello from the probe file", events[0]["summary"])
        self.assertNotIn("\n", events[0]["summary"])  # collapsed to one line

    def test_error_tool_result_reports_not_ok(self):
        raw = {"type": "user", "message": {"content": [
            {"tool_use_id": "t", "type": "tool_result", "is_error": True,
             "content": "boom"}]}}
        self.assertFalse(chat.normalize(raw)[0]["ok"])

    def test_result_closes_the_turn_with_stats(self):
        raw = {"type": "result", "subtype": "success", "is_error": False,
               "result": "done", "session_id": "s1", "duration_ms": 8400,
               "num_turns": 2, "total_cost_usd": 0.32}
        events = chat.normalize(raw)
        self.assertEqual(len(events), 1)
        done = events[0]
        self.assertEqual(done["type"], "turn_done")
        self.assertTrue(done["ok"])
        self.assertEqual(done["result"], "done")
        self.assertEqual(done["stats"], {"durationMs": 8400, "numTurns": 2, "costUsd": 0.32})

    def test_error_result_is_not_ok(self):
        raw = {"type": "result", "subtype": "error_max_turns", "is_error": True}
        self.assertFalse(chat.normalize(raw)[0]["ok"])

    def test_noise_events_are_ignored(self):
        for raw in ({"type": "system", "subtype": "hook_started"},
                    {"type": "rate_limit_event"},
                    {"type": "whatever"},
                    {"type": "assistant", "message": "not-a-dict"}):
            self.assertEqual(chat.normalize(raw), [])


class PostureTests(unittest.TestCase):
    def test_auto_maps_to_bypass(self):
        self.assertEqual(chat.permission_mode("auto"), "bypassPermissions")

    def test_read_only_maps_to_plan(self):
        self.assertEqual(chat.permission_mode("read-only"), "plan")

    def test_unknown_posture_falls_back_to_the_default(self):
        self.assertEqual(chat.permission_mode("nonsense"),
                         chat.permission_mode(chat.DEFAULT_POSTURE))


if __name__ == "__main__":
    unittest.main()
