"""Team engine tests: templating, validation, and the review loop.

Node execution is stubbed with a fake ChatSession that replies from a scripted
function, so the loop control-flow (research -> write -> review -> back to write
until approved, capped) is asserted deterministically with no real `claude`
calls. A separate live check (not here) proves the real integration.
"""

from __future__ import annotations

import queue
import unittest
from unittest import mock

from agentgrid import teams


# --- a fake ChatSession that emits scripted turns synchronously --------------

class FakeSession:
    reply_fn = staticmethod(lambda prompt: "ok")

    def __init__(self, session_id, cwd):
        self.session_id = session_id
        self.cwd = cwd
        self._subs = []

    def subscribe(self):
        channel: queue.Queue = queue.Queue()
        self._subs.append(channel)
        return channel

    def unsubscribe(self, channel):
        if channel in self._subs:
            self._subs.remove(channel)

    def _emit(self, event):
        for channel in self._subs:
            channel.put(event)

    def send(self, prompt, posture, model=""):
        result = FakeSession.reply_fn(prompt)
        self._emit({"type": "turn_started", "sessionId": "fake"})
        self._emit({"type": "assistant_message", "text": result})
        self._emit({"type": "turn_done", "ok": True, "result": result, "stats": {}})

    def cancel(self):
        pass


WRITING = {
    "name": "writing-test",
    "cwd": "/tmp",
    "nodes": [
        {"id": "research", "role": "researcher", "prompt": "RESEARCH: {input}", "posture": "read-only"},
        {"id": "write", "role": "writer", "prompt": "WRITE from {research} fb={review}", "posture": "auto"},
        {"id": "review", "role": "reviewer", "prompt": "REVIEW: {write}", "posture": "read-only"},
    ],
    "loops": [{"at": "review", "back_to": "write", "when": "NEEDS_WORK", "max": 3}],
}


def _run(team, team_input):
    run = teams.TeamRun(team, team_input)
    channel = run.subscribe()
    run.start()
    events = []
    while True:
        event = channel.get(timeout=10)
        events.append(event)
        if event["type"] == "team_done":
            break
    return run, events


def _starts(events, node_id):
    return sum(1 for e in events if e["type"] == "node_started" and e["id"] == node_id)


class TemplatingTests(unittest.TestCase):
    def test_known_placeholders_are_filled(self):
        self.assertEqual(teams.render("hi {input} and {a}", {"input": "X", "a": "Y"}), "hi X and Y")

    def test_unknown_braces_are_left_alone(self):
        self.assertEqual(teams.render("code {notakey} {x}", {"x": "1"}), "code {notakey} 1")


class ValidationTests(unittest.TestCase):
    def test_a_valid_team_loads(self):
        team = teams.load_team(WRITING)
        self.assertEqual([n.id for n in team.nodes], ["research", "write", "review"])
        self.assertEqual(team.loops[0].back_to, "write")

    def test_missing_name_is_rejected(self):
        with self.assertRaises(ValueError):
            teams.load_team({"nodes": [{"id": "a", "prompt": "x"}]})

    def test_no_nodes_is_rejected(self):
        with self.assertRaises(ValueError):
            teams.load_team({"name": "t", "nodes": []})

    def test_duplicate_node_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            teams.load_team({"name": "t", "nodes": [
                {"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}]})

    def test_loop_referencing_unknown_node_is_rejected(self):
        with self.assertRaises(ValueError):
            teams.load_team({"name": "t",
                             "nodes": [{"id": "a", "prompt": "x"}],
                             "loops": [{"at": "a", "back_to": "ghost", "when": "X"}]})


class RunLoopTests(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(teams.chat, "ChatSession", FakeSession)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_review_loop_runs_writer_again_until_approved(self):
        state = {"reviews": 0}

        def reply(prompt):
            if prompt.startswith("RESEARCH"):
                return "research notes"
            if prompt.startswith("WRITE") or prompt.startswith("Please revise"):
                return "a draft"
            if prompt.startswith("REVIEW"):
                state["reviews"] += 1
                return "APPROVED" if state["reviews"] >= 3 else "NEEDS_WORK: tighten it"
            return "?"

        FakeSession.reply_fn = staticmethod(reply)
        run, events = _run(teams.load_team(WRITING), "write about zero")

        self.assertTrue(run.ok)
        self.assertEqual(_starts(events, "research"), 1)
        self.assertEqual(_starts(events, "write"), 3)     # initial + 2 revisions
        self.assertEqual(_starts(events, "review"), 3)
        self.assertEqual(sum(1 for e in events if e["type"] == "loop"), 2)
        self.assertTrue(run.outputs["review"].startswith("APPROVED"))

    def test_loop_is_capped_when_never_approved(self):
        def reply(prompt):
            if prompt.startswith("REVIEW"):
                return "NEEDS_WORK: still off"
            return "x"

        FakeSession.reply_fn = staticmethod(reply)
        capped = dict(WRITING, loops=[{"at": "review", "back_to": "write",
                                       "when": "NEEDS_WORK", "max": 2}])
        run, events = _run(teams.load_team(capped), "topic")

        self.assertEqual(sum(1 for e in events if e["type"] == "loop"), 2)   # exactly max
        self.assertEqual(_starts(events, "write"), 3)                        # initial + 2
        self.assertTrue(run.done and run.ok)

    def test_feedback_reaches_the_writer_on_a_loop(self):
        seen = {"revise_prompt": ""}
        state = {"reviews": 0}

        def reply(prompt):
            if prompt.startswith("Please revise"):
                seen["revise_prompt"] = prompt
                return "revised"
            if prompt.startswith("REVIEW"):
                state["reviews"] += 1
                return "APPROVED" if state["reviews"] >= 2 else "NEEDS_WORK: add a hook"
            return "draft"

        FakeSession.reply_fn = staticmethod(reply)
        _run(teams.load_team(WRITING), "topic")
        self.assertIn("add a hook", seen["revise_prompt"])  # reviewer's note fed back to writer


if __name__ == "__main__":
    unittest.main()
