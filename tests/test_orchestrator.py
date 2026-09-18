"""Orchestrator tests: the definition, the store, and the bounded run loop.

The model is scripted and the machine is faked, so every assertion here is
about the harness rather than about a model's judgement: what it is allowed to
do, what it is stopped from doing, what survives a restart. The fake host
stands in for the server's `ServerHost` -- same six methods -- which is the
point of that boundary existing.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import (areas, credentials, openrouter, orchestrator, orchestrator_brief,
                       orchestrator_run, tickets, web)


# --- a fake machine ----------------------------------------------------------

class FakeHost(orchestrator_run.Host):
    def __init__(self, interactive=True, refuse=""):
        self.caps = {"interactive": interactive, "engines": ["claude", "codex"], "note": ""}
        self.refuse = refuse
        self.started: list[dict] = []
        self.sessions: list[dict] = []
        self.sent: list[tuple] = []
        self.outputs: dict[str, str] = {}

    def capabilities(self):
        return self.caps

    def projects(self):
        return [{"path": "/tmp/project", "name": "project"}]

    def start_agent(self, request):
        if self.refuse:
            raise ValueError(self.refuse)
        self.started.append(request)
        index = len(self.started)
        session = {"sessionId": f"s{index}", "jobId": f"job{index}", "title": request["name"],
                   "customName": request["name"], "status": "working", "engine": request["engine"],
                   "cwd": request["cwd"], "kind": "background", "idleSeconds": 0}
        self.sessions.append(session)
        return {"message": f"Started in {request['cwd']}.", "jobId": session["jobId"],
                "cwd": request["cwd"], "name": request["name"]}

    def agents(self):
        return list(self.sessions)

    def read_output(self, session_id, chars):
        return self.outputs.get(session_id, "")[-chars:]

    def send_to_agent(self, session_id, message):
        self.sent.append((session_id, message))
        return "Queued."


# --- a scripted model --------------------------------------------------------

def turn(text="", calls=(), cost=0.0):
    """One `openrouter.tool_turn` return value, in the shape the run expects."""
    made = []
    for index, (name, args) in enumerate(calls):
        made.append({"id": f"c{index}", "name": name,
                     "arguments": args if isinstance(args, str) else json.dumps(args)})
    raw = {"role": "assistant", "content": text}
    if made:
        raw["tool_calls"] = [{"id": c["id"], "type": "function",
                              "function": {"name": c["name"], "arguments": c["arguments"]}}
                             for c in made]
    return {"text": text, "calls": made, "raw": raw, "usage": {}, "costUsd": cost,
            "finish": "tool_calls" if made else "stop"}


class Model:
    """Replays scripted turns; the final one repeats if the run keeps going."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests: list[dict] = []

    def __call__(self, messages, model, tools, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, "model": model})
        index = min(len(self.requests) - 1, len(self.turns) - 1)
        return self.turns[index]


DEFINITION = {"name": "Shipper", "model": "openai/gpt-test", "instructions": "Ship things.",
              "cwd": "/tmp/project"}


class OrchestratorCase(unittest.TestCase):
    """Isolates every store this touches, so no test writes to real state."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self._patch(orchestrator, "DIR", self.home / "orchestrators")
        self._patch(areas, "PATH", self.home / "areas.json")
        self._patch(areas, "_PENDING", [])
        self._patch(tickets, "TICKETS_DIR", self.home / "tickets")
        self.host = FakeHost()

    def _patch(self, module, attribute, value):
        patcher = mock.patch.object(module, attribute, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, **overrides) -> orchestrator.Orchestrator:
        """Save (or re-save) the test orchestrator, so a test may run twice."""
        body = {**DEFINITION, **overrides}
        existing = next((o for o in orchestrator.list_all()
                         if o.name.casefold() == str(body["name"]).casefold()
                         and o.scope == str(body.get("scope") or "")), None)
        if existing is not None:
            body["id"] = existing.id
        return orchestrator.save(orchestrator.load(body))

    def run_with(self, model, goal="Ship the thing.", **overrides) -> orchestrator_run.Run:
        definition = self.make(**overrides)
        run = orchestrator_run.Run(definition, self.host)
        with mock.patch.object(openrouter, "tool_turn", model):
            run.start(goal)
            self.settle(run)
        return run

    def settle(self, run, timeout=10.0):
        """Wait for the run to come to rest: every resting state is not 'running'."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if run.state["status"] != "running":
                return run.state["status"]
            time.sleep(0.01)
        raise AssertionError(f"run never settled; status={run.state['status']}")


# --- the definition ----------------------------------------------------------

class DefinitionTests(OrchestratorCase):
    def test_a_valid_definition_loads_with_sane_defaults(self):
        definition = orchestrator.load(DEFINITION)
        self.assertEqual(definition.mode, "ask")          # approval is the default
        self.assertEqual(definition.scope, "")            # global unless given an area
        self.assertEqual(definition.limits.max_concurrent, 3)
        self.assertTrue(definition.id)

    def test_a_name_and_a_model_are_both_required(self):
        for body in ({"model": "x/y"}, {"name": "A"}, {"name": "A", "model": "with space"},
                     {"name": "x" * 61, "model": "x/y"}):
            with self.assertRaises(ValueError):
                orchestrator.load(body)

    def test_limits_are_validated_not_trusted(self):
        with self.assertRaises(ValueError):
            orchestrator.load({**DEFINITION, "limits": {"maxConcurrent": 99}})
        with self.assertRaises(ValueError):
            orchestrator.load({**DEFINITION, "limits": {"maxSpendUsd": "lots"}})
        loaded = orchestrator.load({**DEFINITION, "limits": {"maxSpawns": 5, "maxSpendUsd": 1.5}})
        self.assertEqual(loaded.limits.max_spawns, 5)
        self.assertEqual(loaded.limits.max_spend_usd, 1.5)

    def test_names_are_unique_within_a_scope_but_not_across_scopes(self):
        self.make()
        with self.assertRaises(ValueError):
            # A different orchestrator, same scope, a name that only differs in case.
            orchestrator.save(orchestrator.load({**DEFINITION, "name": "shipper"}))
        self.make(scope="engineering")                     # a different area may reuse it
        self.assertEqual(len(orchestrator.list_all()), 2)

    def test_saving_an_edit_keeps_the_same_id(self):
        created = self.make()
        edited = orchestrator.save(orchestrator.load({**DEFINITION, "id": created.id, "mode": "auto"}))
        self.assertEqual(edited.id, created.id)
        self.assertEqual(orchestrator.get(created.id).mode, "auto")
        self.assertEqual(len(orchestrator.list_all()), 1)


class StoreTests(OrchestratorCase):
    def test_state_and_journal_survive_and_are_removed_with_the_definition(self):
        definition = self.make()
        orchestrator.write_state(definition.id, {**orchestrator.blank_state(), "status": "running"})
        orchestrator.append_journal(definition.id, {"type": "note", "text": "hello"})
        self.assertTrue(orchestrator.interrupted(orchestrator.read_state(definition.id)))
        self.assertEqual(orchestrator.journal(definition.id)[0]["text"], "hello")
        orchestrator.delete(definition.id)
        self.assertEqual(orchestrator.list_all(), [])
        self.assertEqual(orchestrator.journal(definition.id), [])
        self.assertEqual(orchestrator.read_state(definition.id)["status"], "idle")

    def test_an_unreadable_definition_is_skipped_not_fatal(self):
        good = self.make()
        broken = orchestrator.DIR / "deadbeef"
        broken.mkdir(parents=True)
        (broken / "definition.json").write_text("{not json", encoding="utf-8")
        self.assertEqual([o.id for o in orchestrator.list_all()], [good.id])

    def test_a_waiting_run_is_not_treated_as_interrupted(self):
        self.assertFalse(orchestrator.interrupted({"status": "waiting"}))


# --- the tool menu -----------------------------------------------------------

class MenuTests(OrchestratorCase):
    def names(self, definition, capabilities):
        return [spec["function"]["name"]
                for spec in orchestrator_brief.tool_specs(definition, capabilities)]

    def test_a_host_without_a_terminal_is_not_offered_interactive(self):
        definition = orchestrator.load(DEFINITION)
        specs = orchestrator_brief.tool_specs(definition, {"interactive": False, "engines": ["claude"]})
        start = next(s for s in specs if s["function"]["name"] == "start_agent")
        self.assertEqual(start["function"]["parameters"]["properties"]["mode"]["enum"], ["background"])
        with_terminal = orchestrator_brief.tool_specs(definition, {"interactive": True, "engines": ["claude"]})
        start = next(s for s in with_terminal if s["function"]["name"] == "start_agent")
        self.assertIn("interactive", start["function"]["parameters"]["properties"]["mode"]["enum"])

    def test_a_scoped_orchestrator_is_not_offered_a_choice_of_work_area(self):
        definition = orchestrator.load({**DEFINITION, "scope": "engineering"})
        specs = orchestrator_brief.tool_specs(definition, self.host.caps)
        start = next(s for s in specs if s["function"]["name"] == "start_agent")
        self.assertNotIn("areaId", start["function"]["parameters"]["properties"])
        glob = orchestrator_brief.tool_specs(orchestrator.load(DEFINITION), self.host.caps)
        start = next(s for s in glob if s["function"]["name"] == "start_agent")
        self.assertIn("areaId", start["function"]["parameters"]["properties"])

    def test_only_a_global_orchestrator_can_create_work_areas(self):
        self.assertIn("create_work_area", self.names(orchestrator.load(DEFINITION), self.host.caps))
        scoped = orchestrator.load({**DEFINITION, "scope": "engineering"})
        self.assertNotIn("create_work_area", self.names(scoped, self.host.caps))

    def test_every_tool_the_menu_offers_has_a_handler(self):
        run = orchestrator_run.Run(orchestrator.load(DEFINITION), self.host)
        for name in self.names(run.definition, self.host.caps):
            self.assertTrue(hasattr(run, "_tool_" + name), name)

    def test_the_prompt_states_the_mode_the_limits_and_the_scope(self):
        prompt = orchestrator_brief.system_prompt(orchestrator.load(DEFINITION), self.host.caps, "every area")
        self.assertIn("approval every time", prompt)
        self.assertIn("$5.00", prompt)
        self.assertIn("every area", prompt)
        auto = orchestrator_brief.system_prompt(orchestrator.load({**DEFINITION, "mode": "auto"}),
                                              self.host.caps, "every area")
        self.assertIn("without asking", auto)


# --- running: auto mode ------------------------------------------------------

class AutoModeTests(OrchestratorCase):
    def test_it_starts_an_agent_and_finishes(self):
        model = Model(
            turn("Starting.", [("start_agent", {"project": "/tmp/project", "task": "Do it",
                                                "name": "worker"})]),
            turn("", [("finish", {"summary": "Shipped."})]),
        )
        run = self.run_with(model, mode="auto")
        self.assertEqual(run.state["status"], "done")
        self.assertEqual(run.state["reason"], "Shipped.")
        self.assertEqual(len(self.host.started), 1)
        self.assertEqual(self.host.started[0]["prompt"], "Do it")
        self.assertFalse(self.host.started[0]["interactive"])
        self.assertEqual(run.state["spawns"], 1)
        self.assertEqual(run.state["children"][0]["name"], "worker")
        kinds = [event["type"] for event in orchestrator.journal(run.definition.id)]
        self.assertEqual(kinds[0], "run_started")
        self.assertIn("agent_started", kinds)
        self.assertEqual(kinds[-1], "run_done")

    def test_the_goal_and_the_system_prompt_open_the_conversation(self):
        model = Model(turn("", [("finish", {"summary": "done"})]))
        run = self.run_with(model, mode="auto", goal="Ship X")
        first = model.requests[0]["messages"]
        self.assertEqual(first[0]["role"], "system")
        self.assertIn("Shipper", first[0]["content"])
        self.assertEqual(first[1], {"role": "user", "content": "Goal:\nShip X"})

    def test_a_scoped_orchestrator_puts_its_agents_in_its_own_area(self):
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "t"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        self.run_with(model, mode="auto", scope="engineering")
        self.assertEqual(self.host.started[0]["areaId"], "engineering")

    def test_an_unnamed_agent_still_gets_a_name_to_find_it_by(self):
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "t"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        run = self.run_with(model, mode="auto")
        self.assertEqual(self.host.started[0]["name"], "Shipper 1")
        self.assertEqual(run.state["children"][0]["name"], "Shipper 1")

    def test_a_refused_launch_reaches_the_model_as_a_failed_tool(self):
        self.host.refuse = "That directory is not one of the known projects."
        model = Model(turn("", [("start_agent", {"project": "/etc", "task": "t"})]),
                      turn("", [("finish", {"summary": "could not start"})]))
        run = self.run_with(model, mode="auto")
        self.assertEqual(run.state["status"], "done")
        results = [m for m in run.state["messages"] if m.get("role") == "tool"]
        self.assertIn("not one of the known projects", results[0]["content"])
        self.assertEqual(run.state["spawns"], 0)


class LimitTests(OrchestratorCase):
    def test_concurrency_is_refused_without_calling_the_host(self):
        model = Model(
            turn("", [("start_agent", {"project": "/tmp/project", "task": "one", "name": "a"})]),
            turn("", [("start_agent", {"project": "/tmp/project", "task": "two", "name": "b"})]),
            turn("", [("finish", {"summary": "waited"})]),
        )
        run = self.run_with(model, mode="auto", limits={"maxConcurrent": 1})
        self.assertEqual(len(self.host.started), 1)
        refusal = [m for m in run.state["messages"] if m.get("role") == "tool"][1]["content"]
        self.assertIn("still running", refusal)
        self.assertIn("wait_for_agents", refusal)

    def test_the_spawn_limit_caps_one_run(self):
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "t"})]),
                      turn("", [("finish", {"summary": "stop"})]))
        run = self.run_with(model, mode="auto", limits={"maxSpawns": 1, "maxConcurrent": 5})
        self.assertEqual(run.state["spawns"], 1)
        # A second attempt in the same run is refused by the harness.
        with mock.patch.object(openrouter, "tool_turn", model):
            run.submit("start another")
            self.settle(run)
        self.assertEqual(len(self.host.started), 1)

    def test_the_step_limit_stops_the_run(self):
        model = Model(turn("", [("message_user", {"text": "still going"})]))
        run = self.run_with(model, mode="auto", limits={"maxSteps": 2})
        self.assertEqual(run.state["status"], "failed")
        self.assertIn("Step limit", run.state["reason"])
        self.assertEqual(len(model.requests), 2)

    def test_the_spend_limit_stops_the_run(self):
        model = Model(turn("", [("message_user", {"text": "expensive"})], cost=0.40))
        run = self.run_with(model, mode="auto", limits={"maxSpendUsd": 0.25})
        self.assertEqual(run.state["status"], "failed")
        self.assertIn("Spend limit", run.state["reason"])
        self.assertAlmostEqual(run.state["costUsd"], 0.40, places=4)


# --- running: ask mode and approvals ----------------------------------------

class ApprovalTests(OrchestratorCase):
    def script(self):
        return Model(
            turn("Requesting an agent.", [("start_agent", {"project": "/tmp/project",
                                                           "task": "Do it", "name": "worker"})]),
            turn("", [("finish", {"summary": "Done."})]),
        )

    def test_ask_mode_pauses_before_starting_anything(self):
        model = self.script()
        run = self.run_with(model)
        self.assertEqual(run.state["status"], "waiting")
        self.assertEqual(self.host.started, [])
        pending = run.snapshot()["pending"]
        self.assertEqual(pending["tool"], "start_agent")
        self.assertEqual(pending["reason"], "starts a new agent")
        self.assertEqual(pending["args"]["task"], "Do it")
        self.assertEqual(len(model.requests), 1)         # no further spend while it waits

    def test_approving_starts_it_and_the_run_continues(self):
        model = self.script()
        run = self.run_with(model)
        with mock.patch.object(openrouter, "tool_turn", model):
            run.approve(run.snapshot()["pending"]["id"])
            self.settle(run)
        self.assertEqual(len(self.host.started), 1)
        self.assertEqual(run.state["status"], "done")
        self.assertIsNone(run.state["pending"])
        kinds = [event["type"] for event in orchestrator.journal(run.definition.id)]
        self.assertIn("approval_requested", kinds)
        self.assertIn("approval_resolved", kinds)

    def test_editing_before_approving_changes_what_runs(self):
        model = self.script()
        run = self.run_with(model)
        with mock.patch.object(openrouter, "tool_turn", model):
            run.approve(run.snapshot()["pending"]["id"],
                        {"task": "Do it carefully", "name": "edited"})
            self.settle(run)
        self.assertEqual(self.host.started[0]["prompt"], "Do it carefully")
        self.assertEqual(self.host.started[0]["name"], "edited")

    def test_declining_tells_the_model_why_and_starts_nothing(self):
        model = self.script()
        run = self.run_with(model)
        with mock.patch.object(openrouter, "tool_turn", model):
            run.decline(run.snapshot()["pending"]["id"], "wrong project")
            self.settle(run)
        self.assertEqual(self.host.started, [])
        declined = [m for m in run.state["messages"] if m.get("role") == "tool"][0]["content"]
        self.assertIn("declined", declined)
        self.assertIn("wrong project", declined)
        self.assertEqual(run.state["status"], "done")

    def test_a_stale_approval_id_is_refused(self):
        run = self.run_with(self.script())
        with self.assertRaises(ValueError):
            run.approve("not-the-pending-one")
        with self.assertRaises(ValueError):
            orchestrator_run.Run(self.make(name="Other"), self.host).approve("x")

    def test_the_rest_of_a_turn_runs_after_the_approval(self):
        """Two calls in one turn: the gated one pauses, the other still happens."""
        model = Model(
            turn("", [("start_agent", {"project": "/tmp/project", "task": "t", "name": "w"}),
                      ("set_plan", {"items": [{"text": "check the work"}]})]),
            turn("", [("finish", {"summary": "ok"})]),
        )
        run = self.run_with(model)
        self.assertEqual(run.state["plan"], [])
        with mock.patch.object(openrouter, "tool_turn", model):
            run.approve(run.snapshot()["pending"]["id"])
            self.settle(run)
        self.assertEqual(run.state["plan"], [{"text": "check the work", "done": False}])
        self.assertEqual(len(self.host.started), 1)

    def test_auto_mode_still_asks_before_opening_a_terminal_window(self):
        model = Model(
            turn("", [("start_agent", {"project": "/tmp/project", "task": "t",
                                       "mode": "interactive"})]),
            turn("", [("finish", {"summary": "ok"})]),
        )
        run = self.run_with(model, mode="auto")
        self.assertEqual(run.state["status"], "waiting")
        self.assertEqual(self.host.started, [])
        self.assertIn("interactive Terminal", run.snapshot()["pending"]["reason"])

    def test_an_approval_outlives_the_process(self):
        """A pending approval is read back from disk, then answered."""
        model = self.script()
        first = self.run_with(model)
        reloaded = orchestrator_run.Run(orchestrator.get(first.definition.id), self.host)
        self.assertEqual(reloaded.state["status"], "waiting")
        with mock.patch.object(openrouter, "tool_turn", model):
            reloaded.approve(reloaded.snapshot()["pending"]["id"])
            self.settle(reloaded)
        self.assertEqual(len(self.host.started), 1)
        self.assertEqual(reloaded.state["status"], "done")


# --- talking to the user -----------------------------------------------------

class ConversationTests(OrchestratorCase):
    def test_plain_prose_hands_the_turn_back_to_the_user(self):
        model = Model(turn("Which repo did you mean?"))
        run = self.run_with(model)
        self.assertEqual(run.state["status"], "waiting")
        self.assertIsNone(run.state["pending"])
        self.assertEqual(run.state["lastText"], "Which repo did you mean?")
        self.assertEqual(len(model.requests), 1)

    def test_a_reply_resumes_the_run(self):
        model = Model(turn("Which repo?"), turn("", [("finish", {"summary": "ok"})]))
        run = self.run_with(model)
        with mock.patch.object(openrouter, "tool_turn", model):
            run.submit("the second one")
            self.settle(run)
        self.assertEqual(run.state["status"], "done")
        self.assertIn({"role": "user", "content": "the second one"}, run.state["messages"])

    def test_a_message_while_an_approval_waits_is_queued_not_acted_on(self):
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "t"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        run = self.run_with(model)
        run.submit("hold on")
        self.assertEqual(run.state["status"], "waiting")
        self.assertEqual(run.snapshot()["queued"], 1)
        self.assertEqual(len(model.requests), 1)

    def test_a_finished_run_can_be_picked_up_without_ending_at_once(self):
        """The finish summary is consumed: left armed it would end the next run
        at its first tool call, with the previous run's words."""
        run = self.run_with(Model(turn("", [("finish", {"summary": "first"})])), mode="auto")
        self.assertEqual(run.state["reason"], "first")
        again = Model(turn("", [("list_agents", {})]),
                      turn("", [("finish", {"summary": "second"})]))
        with mock.patch.object(openrouter, "tool_turn", again):
            run.submit("keep going")
            self.settle(run)
        self.assertEqual(len(again.requests), 2)
        self.assertEqual(run.state["reason"], "second")

    def test_only_one_worker_ever_owns_a_run(self):
        """Two workers would double every step, and a thread on its way out is
        still alive -- so the flag, not the thread, is what is checked."""
        run = orchestrator_run.Run(self.make(), self.host)
        run._active = True
        run._spin()
        self.assertIsNone(run._worker)

    def test_stopping_a_run_ends_it(self):
        model = Model(turn("waiting on you"))
        run = self.run_with(model)
        run.stop()
        self.assertEqual(run.state["status"], "stopped")
        self.assertEqual([e["type"] for e in orchestrator.journal(run.definition.id)][-1], "run_done")


# --- watching its agents, for free ------------------------------------------

class WatchingTests(OrchestratorCase):
    """The orchestrator should never need to be asked how it is going.

    These pin the two halves of that: prose while agents work is a progress
    note rather than a pause, and AgentGrid -- not the model -- does the
    waiting, waking it only when an agent actually changes.
    """

    def setUp(self):
        super().setUp()
        self._patch(orchestrator_run, "WATCH_POLL_SECONDS", 0.01)

    def later(self, seconds, change):
        timer = threading.Timer(seconds, change)
        timer.start()
        self.addCleanup(timer.cancel)

    def started_then_prose(self, *after):
        return Model(
            turn("", [("start_agent", {"project": "/tmp/project", "task": "t", "name": "worker"})]),
            turn("Started worker; I'll report when it's done."),
            *after,
        )

    def test_prose_while_an_agent_works_is_a_progress_note_not_a_pause(self):
        self.host.outputs["s1"] = "All tests pass."
        self.later(0.2, lambda: self.host.sessions[0].update(status="done"))
        model = self.started_then_prose(turn("", [("finish", {"summary": "worker finished"})]))
        run = self.run_with(model, mode="auto")
        self.assertEqual(run.state["status"], "done")
        self.assertEqual(len(model.requests), 3)                # no call was spent polling
        journal = orchestrator.journal(run.definition.id)
        notes = [e for e in journal if e["type"] == "message"]
        self.assertEqual(notes[0]["text"], "Started worker; I'll report when it's done.")
        self.assertFalse(notes[0]["waiting"])
        update = next(e for e in journal if e["type"] == "agent_update")
        self.assertEqual((update["name"], update["from"], update["to"]), ("worker", "working", "done"))

    def test_the_model_is_woken_with_what_changed_and_what_the_agent_said(self):
        self.host.outputs["s1"] = "Fixed the parser; 12 tests added."
        self.later(0.2, lambda: self.host.sessions[0].update(status="blocked"))
        model = self.started_then_prose(turn("", [("finish", {"summary": "ok"})]))
        self.run_with(model, mode="auto")
        woken = model.requests[2]["messages"][-1]
        self.assertEqual(woken["role"], "user")
        self.assertIn("worker: working → blocked", woken["content"])
        self.assertIn("12 tests added", woken["content"])
        self.assertIn("message_user", woken["content"])

    def test_prose_with_nothing_running_still_hands_the_turn_to_the_user(self):
        run = self.run_with(Model(turn("Which repo did you mean?")), mode="auto")
        self.assertEqual(run.state["status"], "waiting")
        self.assertTrue([e for e in orchestrator.journal(run.definition.id)
                         if e["type"] == "message"][0]["waiting"])

    def test_a_finished_agent_is_not_waited_on(self):
        self.host.sessions.append({"sessionId": "s9", "jobId": "job9", "title": "old",
                                   "customName": "old", "status": "done"})
        run = orchestrator_run.Run(self.make(mode="auto"), self.host)
        run.state["children"] = [{"name": "old", "jobId": "job9", "at": time.time()}]
        model = Model(turn("All done here."))
        with mock.patch.object(openrouter, "tool_turn", model):
            run.state.update(status="running", messages=[{"role": "system", "content": "s"},
                                                         {"role": "user", "content": "g"}])
            run._spin()
            self.settle(run)
        self.assertEqual(run.state["status"], "waiting")

    def test_being_picked_up_is_not_news_but_disappearing_is(self):
        run = orchestrator_run.Run(self.make(mode="auto"), self.host)
        long_ago = time.time() - orchestrator_run.CHILD_GRACE_SECONDS - 5
        run.state["children"] = [{"name": "fresh", "jobId": "jobF", "at": time.time()},
                                 {"name": "lost", "jobId": "jobL", "at": long_ago}]
        statuses = run._child_statuses()
        self.assertEqual(statuses, {"fresh": "starting", "lost": "gone"})
        # starting -> working is the expected pickup: the watch keeps going
        self.host.sessions.append({"sessionId": "sF", "jobId": "jobF", "title": "fresh",
                                   "customName": "fresh", "status": "working"})
        self.assertEqual(run._watch(limit=0.1, baseline=statuses), [])

    def test_wait_for_agents_returns_the_moment_something_changes(self):
        self.host.outputs["s1"] = "done and dusted"
        run = self.run_one_agent()
        self.later(0.15, lambda: self.host.sessions[0].update(status="done"))
        started = time.monotonic()
        result = run._tool_wait_for_agents({"seconds": 30})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["changed"][0]["to"], "done")
        self.assertEqual(result["changed"][0]["output"], "done and dusted")
        self.assertFalse(result["interrupted"])

    def test_a_message_from_the_user_cuts_the_watch_short(self):
        model = self.started_then_prose(turn("", [("finish", {"summary": "answered"})]))
        definition = self.make(mode="auto")
        run = orchestrator_run.Run(definition, self.host)
        with mock.patch.object(openrouter, "tool_turn", model):
            run.start("Ship it")
            deadline = time.monotonic() + 5
            while run.phase != "watching" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(run.snapshot()["phase"], "watching")
            self.assertEqual(run.snapshot()["busyAgents"], 1)
            run.submit("how is it going?")
            self.settle(run)
        self.assertEqual(run.state["status"], "done")
        self.assertIn({"role": "user", "content": "how is it going?"}, model.requests[2]["messages"])

    def test_the_brief_tells_it_not_to_poll_and_to_report_unasked(self):
        prompt = orchestrator_brief.system_prompt(orchestrator.load(DEFINITION), self.host.caps, "x")
        self.assertIn("Never poll", prompt)
        self.assertIn("should never have to ask", prompt)

    def run_one_agent(self):
        run = orchestrator_run.Run(self.make(mode="auto"), self.host)
        run.state.update(status="running")
        run._tool_start_agent({"project": "/tmp/project", "task": "t", "name": "worker"})
        return run


class FollowUpTests(unittest.TestCase):
    """A follow-up that could not arrive is refused, never silently forked."""

    def session(self, status):
        return SimpleNamespace(session_id="s1", status=status, engine="claude",
                               cwd="/tmp", display_title="worker", pid=None)

    def test_a_working_agent_refuses_a_follow_up(self):
        chat = SimpleNamespace(state=lambda sid: {"running": False})
        self.assertIn("separate copy", web.follow_up_block(self.session("working"), chat))

    def test_an_agent_between_turns_takes_one(self):
        chat = SimpleNamespace(state=lambda sid: {"running": False})
        for status in ("done", "idle", "blocked"):
            self.assertIsNone(web.follow_up_block(self.session(status), chat))

    def test_a_turn_this_server_is_running_is_fine_to_queue_behind(self):
        chat = SimpleNamespace(state=lambda sid: {"running": True})
        self.assertIsNone(web.follow_up_block(self.session("working"), chat))

    def test_the_host_refuses_rather_than_forking(self):
        sent = []
        chat = SimpleNamespace(state=lambda sid: {"running": False},
                               send=lambda *a, **k: sent.append(a))
        fleet = SimpleNamespace(raw=lambda: [self.session("working")])
        host = web.ServerHost(fleet, chat)
        with self.assertRaises(ValueError):
            host.send_to_agent("s1", "also add docs")
        self.assertEqual(sent, [])


# --- the tools ---------------------------------------------------------------

class ToolTests(OrchestratorCase):
    def run_one(self, calls, **overrides):
        model = Model(turn("", calls), turn("", [("finish", {"summary": "ok"})]))
        return self.run_with(model, mode="auto", **overrides)

    def results(self, run):
        return [json.loads(m["content"]) for m in run.state["messages"] if m.get("role") == "tool"]

    def test_malformed_arguments_are_reported_back_rather_than_crashing(self):
        run = self.run_one([("set_plan", "{not json")])
        self.assertFalse(self.results(run)[0]["ok"])
        self.assertIn("valid JSON", self.results(run)[0]["error"])
        self.assertEqual(run.state["status"], "done")

    def test_an_unknown_tool_is_reported_back(self):
        run = self.run_one([("rm_rf", {})])
        self.assertIn("no tool called", self.results(run)[0]["error"])

    def test_list_projects_and_list_agents_describe_the_machine(self):
        run = self.run_one([("list_projects", {}), ("list_agents", {})])
        self.assertEqual(self.results(run)[0]["projects"], [{"path": "/tmp/project", "name": "project"}])
        self.assertEqual(self.results(run)[1]["agents"], [])

    def test_an_agent_can_be_read_and_messaged_by_the_name_it_was_given(self):
        self.host.outputs["s1"] = "x" * 100 + "the tail"
        run = self.run_one([
            ("start_agent", {"project": "/tmp/project", "task": "t", "name": "worker"}),
            ("read_agent_output", {"agent": "worker", "chars": 500}),
            ("send_to_agent", {"agent": "worker", "message": "also fix the tests"}),
        ])
        read, sent = self.results(run)[1], self.results(run)[2]
        self.assertTrue(read["output"].endswith("the tail"))
        self.assertEqual(read["sessionId"], "s1")
        self.assertEqual(self.host.sent, [("s1", "also fix the tests")])
        self.assertTrue(sent["ok"])
        self.assertEqual(run.state["children"][0]["sessionId"], "s1")

    def test_an_unknown_agent_name_lists_what_is_actually_running(self):
        run = self.run_one([("start_agent", {"project": "/tmp/project", "task": "t", "name": "worker"}),
                            ("read_agent_output", {"agent": "ghost"})])
        error = self.results(run)[1]["error"]
        self.assertIn("ghost", error)
        self.assertIn("worker", error)

    def test_waiting_is_interrupted_by_the_user(self):
        run = orchestrator_run.Run(self.make(mode="auto"), self.host)
        started = time.monotonic()
        run._wake.set()
        result = run._tool_wait_for_agents({"seconds": 30})
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result["interrupted"])

    def test_the_plan_is_stored_and_shown(self):
        run = self.run_one([("set_plan", {"items": [{"text": "one", "done": True},
                                                    {"text": "two"}]})])
        self.assertEqual(run.state["plan"], [{"text": "one", "done": True},
                                             {"text": "two", "done": False}])
        self.assertEqual(run.snapshot()["plan"][0]["text"], "one")

    def test_a_message_to_the_user_is_journalled_and_kept(self):
        run = self.run_one([("message_user", {"text": "two agents started"})])
        self.assertEqual(run.state["lastText"], "two agents started")
        messages = [e for e in orchestrator.journal(run.definition.id) if e["type"] == "message"]
        self.assertEqual(messages[0]["text"], "two agents started")

    def test_tickets_are_filed_read_moved_and_commented_under_its_own_name(self):
        run = self.run_one([("create_ticket", {"title": "Fix the parser", "priority": "high"})])
        ticket_id = self.results(run)[0]["ticket"]["id"]
        stored = tickets.get(ticket_id)
        self.assertEqual(stored["reporter"], "Shipper")
        self.assertEqual(stored["priority"], "high")
        run = self.run_one([("list_tickets", {}),
                            ("move_ticket", {"ticket": ticket_id, "status": "in_progress"}),
                            ("comment_ticket", {"ticket": ticket_id, "text": "agent is on it"})])
        listed, moved = self.results(run)[0], self.results(run)[1]
        self.assertEqual([t["id"] for t in listed["tickets"]], [ticket_id])
        self.assertEqual(moved["ticket"]["status"], "in_progress")
        comments = [a for a in tickets.get(ticket_id)["activity"] if a["kind"] == "comment"]
        self.assertEqual((comments[0]["who"], comments[0]["text"]), ("Shipper", "agent is on it"))

    def test_a_scoped_orchestrator_cannot_file_work_into_another_area(self):
        """Its own area wins over whatever it asks for."""
        run = self.run_one([("start_agent", {"project": "/tmp/project", "task": "t",
                                             "areaId": "design"})], scope="engineering")
        self.assertEqual(self.host.started[0]["areaId"], "engineering")

    def test_a_host_without_a_terminal_refuses_interactive_before_asking_you(self):
        self.host.caps = {**self.host.caps, "interactive": False}
        run = self.run_one([("start_agent", {"project": "/tmp/project", "task": "t",
                                             "mode": "interactive"})])
        self.assertEqual(self.host.started, [])
        self.assertIsNone(run.state["pending"])          # no pointless approval
        self.assertIn("cannot open interactive", self.results(run)[0]["error"])

    def test_a_scoped_orchestrator_cannot_create_work_areas(self):
        run = self.run_one([("create_work_area", {"name": "Research"})], scope="engineering")
        self.assertFalse(self.results(run)[0]["ok"])
        self.assertIn("cannot create", self.results(run)[0]["error"])

    def test_the_global_orchestrator_can_create_a_work_area(self):
        run = self.run_one([("create_work_area", {"name": "Research"})])
        self.assertEqual(self.results(run)[0]["area"]["name"], "Research")
        self.assertIn("Research", [a["name"] for a in areas.load()["areas"]])

    def test_a_huge_tool_result_is_truncated_before_it_becomes_context(self):
        self.host.outputs["s1"] = "y" * (orchestrator_run.MAX_TOOL_RESULT * 3)
        run = self.run_one([("start_agent", {"project": "/tmp/project", "task": "t", "name": "w"}),
                            ("read_agent_output", {"agent": "w", "chars": 999_999})])
        payload = [m for m in run.state["messages"] if m.get("role") == "tool"][1]["content"]
        self.assertLessEqual(len(payload), orchestrator_run.MAX_TOOL_RESULT + 200)
        self.assertTrue(json.loads(payload)["truncated"])


# --- surviving a restart -----------------------------------------------------

class ResumeTests(OrchestratorCase):
    def test_repair_drops_only_an_unanswered_turn(self):
        answered = [{"role": "assistant", "tool_calls": [{"id": "a"}]},
                    {"role": "tool", "tool_call_id": "a", "content": "{}"}]
        self.assertEqual(orchestrator_run.repair_messages(list(answered)), answered)
        unanswered = answered + [{"role": "assistant", "tool_calls": [{"id": "b"}]}]
        self.assertEqual(orchestrator_run.repair_messages(unanswered), answered)
        partial = answered + [{"role": "assistant", "tool_calls": [{"id": "c"}, {"id": "d"}]},
                              {"role": "tool", "tool_call_id": "c", "content": "{}"}]
        self.assertEqual(orchestrator_run.repair_messages(partial), answered)

    def test_an_interrupted_run_resumes_and_is_told_to_look_first(self):
        definition = self.make(mode="auto")
        orchestrator.write_state(definition.id, {
            **orchestrator.blank_state(), "status": "running", "goal": "Ship",
            "steps": 4, "spawns": 1,
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "Goal:\nShip"},
                         {"role": "assistant", "tool_calls": [{"id": "z"}]}]})
        model = Model(turn("", [("finish", {"summary": "picked up"})]))
        manager = orchestrator_run.RunManager(self.host)
        with mock.patch.object(openrouter, "tool_turn", model):
            self.assertEqual(manager.resume_all(), ["Shipper"])
            run = manager.get(definition.id)
            self.settle(run)
        self.assertEqual(run.state["status"], "done")
        sent = model.requests[0]["messages"]
        self.assertNotIn("assistant", [m["role"] for m in sent])   # the dangling turn is gone
        self.assertIn("interrupted", sent[-1]["content"])
        self.assertIn("list_agents", sent[-1]["content"])
        self.assertEqual(run.state["steps"], 5)                    # counters carried over

    def test_a_run_at_rest_is_not_resumed(self):
        definition = self.make()
        orchestrator.write_state(definition.id, {**orchestrator.blank_state(), "status": "waiting"})
        manager = orchestrator_run.RunManager(self.host)
        self.assertEqual(manager.resume_all(), [])

    def test_a_deleted_orchestrator_stops_writing_state(self):
        definition = self.make(mode="auto")
        manager = orchestrator_run.RunManager(self.host)
        run = manager.run(definition)
        manager.forget(definition.id)
        orchestrator.delete(definition.id)
        run._persist()                       # a late worker write must not land
        self.assertFalse((orchestrator.DIR / definition.id).exists())

    def test_a_child_started_just_before_the_interruption_is_not_lost(self):
        """The child record is written before the model hears about it."""
        definition = self.make(mode="auto")
        run = orchestrator_run.Run(definition, self.host)
        run.state.update(status="running", messages=[{"role": "system", "content": "s"},
                                                     {"role": "user", "content": "g"}])
        run._tool_start_agent({"project": "/tmp/project", "task": "t", "name": "early"})
        self.assertEqual(orchestrator.read_state(definition.id)["children"][0]["name"], "early")
        self.assertEqual(orchestrator.read_state(definition.id)["spawns"], 1)


class ContextTests(unittest.TestCase):
    def test_trimming_keeps_the_system_prompt_the_goal_and_the_tail(self):
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "goal"}]
        messages += [{"role": "assistant", "content": "x" * 1000} for _ in range(50)]
        trimmed = orchestrator_run.trim_messages(messages, budget=5000)
        self.assertEqual(trimmed[0]["content"], "sys")
        self.assertEqual(trimmed[1]["content"], "goal")
        self.assertLess(len(trimmed), len(messages))
        self.assertEqual(trimmed[-1], messages[-1])

    def test_trimming_never_starts_the_tail_on_an_orphan_tool_result(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "g"}]
        messages += [{"role": "assistant", "content": "a" * 4000,
                      "tool_calls": [{"id": "1"}]},
                     {"role": "tool", "tool_call_id": "1", "content": "r"},
                     {"role": "assistant", "content": "b"}]
        trimmed = orchestrator_run.trim_messages(messages, budget=200)
        self.assertNotEqual(trimmed[2]["role"], "tool")


# --- the HTTP surface --------------------------------------------------------

class RouteTests(OrchestratorCase):
    def handler(self):
        handler = object.__new__(web.Handler)
        handler.fleet = SimpleNamespace(raw=lambda: [], snapshot=lambda: {"sessions": []})
        handler.chat = SimpleNamespace(state=lambda sid: {}, send=lambda *a, **k: None)
        handler.runs = orchestrator_run.RunManager(self.host)
        self.replies = []
        handler._send_json = lambda code, data: self.replies.append((code, data))
        return handler

    def last(self):
        return self.replies[-1]

    def test_save_list_and_delete(self):
        handler = self.handler()
        handler._orchestrator_action("save", DEFINITION)
        code, payload = self.last()
        self.assertEqual(code, 200)
        identifier = payload["orchestrator"]["id"]
        handler._orchestrators()
        code, payload = self.last()
        self.assertEqual([o["name"] for o in payload["orchestrators"]], ["Shipper"])
        self.assertEqual(payload["capabilities"], web.host_capabilities())
        handler._orchestrator_action("delete", {"id": identifier})
        handler._orchestrators()
        self.assertEqual(self.last()[1]["orchestrators"], [])

    def test_a_bad_definition_is_a_400_and_saves_nothing(self):
        handler = self.handler()
        handler._orchestrator_action("save", {"name": "No model"})
        self.assertEqual(self.last()[0], 400)
        self.assertEqual(orchestrator.list_all(), [])

    def test_unknown_ids_and_actions_are_refused(self):
        handler = self.handler()
        handler._orchestrator_action("start", {"id": "nope", "goal": "x"})
        self.assertEqual(self.last()[0], 400)
        handler._orchestrator_action("delete", {"id": "nope"})
        self.assertEqual(self.last()[0], 400)
        self.make()
        handler._orchestrator_action("teleport", {"id": orchestrator.list_all()[0].id})
        self.assertEqual(self.last()[0], 404)

    def test_starting_needs_openrouter_connected(self):
        handler = self.handler()
        definition = self.make()
        with mock.patch.object(credentials, "get_key", return_value=""):
            handler._orchestrator_action("start", {"id": definition.id, "goal": "Ship"})
        self.assertEqual(self.last()[0], 400)
        self.assertIn("Connect OpenRouter", self.last()[1]["error"])

    def test_start_and_approve_over_the_api(self):
        handler = self.handler()
        definition = self.make()
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "t"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        with mock.patch.object(credentials, "get_key", return_value="key"), \
                mock.patch.object(openrouter, "tool_turn", model):
            handler._orchestrator_action("start", {"id": definition.id, "goal": "Ship"})
            run = handler.runs.get(definition.id)
            self.settle(run)
            self.assertEqual(self.last()[0], 200)
            pending = run.snapshot()["pending"]
            handler._orchestrator_action("approve", {"id": definition.id,
                                                     "approvalId": pending["id"]})
            self.settle(run)
        self.assertEqual(len(self.host.started), 1)
        # The POST answers as soon as the run is unblocked, so the finished
        # status is read from the run rather than from that reply.
        self.assertEqual(run.state["status"], "done")

    def test_the_journal_route_replays_what_happened(self):
        handler = self.handler()
        definition = self.make(mode="auto")
        model = Model(turn("", [("finish", {"summary": "ok"})]))
        with mock.patch.object(credentials, "get_key", return_value="key"), \
                mock.patch.object(openrouter, "tool_turn", model):
            handler._orchestrator_action("start", {"id": definition.id, "goal": "Ship"})
            self.settle(handler.runs.get(definition.id))
        handler._orchestrator_journal({"id": [definition.id]})
        code, payload = self.last()
        self.assertEqual(code, 200)
        self.assertEqual(payload["events"][0]["type"], "run_started")
        self.assertEqual(payload["orchestrator"]["status"], "done")

    def test_editing_the_mode_applies_to_a_run_in_flight(self):
        """The run reloads its definition on every request, which is what makes
        the Ask me / Auto switch usable without stopping the work."""
        handler = self.handler()
        definition = self.make()
        model = Model(turn("Which project?"))
        with mock.patch.object(credentials, "get_key", return_value="key"), \
                mock.patch.object(openrouter, "tool_turn", model):
            handler._orchestrator_action("start", {"id": definition.id, "goal": "Ship"})
            self.settle(handler.runs.get(definition.id))
        handler._orchestrator_action("save", {**DEFINITION, "id": definition.id, "mode": "auto"})
        self.assertEqual(handler.runs.get(definition.id).definition.mode, "auto")


if __name__ == "__main__":
    unittest.main()
