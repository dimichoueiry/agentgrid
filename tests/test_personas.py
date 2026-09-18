"""Personas: who an orchestrator is, reused across the teams it is posted to.

Pins the promises the feature was built for: defaults you set once and never
repeat, a fence the model cannot talk its way past, saved agents started as
saved, memory that lands in the right place and survives Start again, and
older orchestrators converted without losing anything.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import (credentials, openrouter, orchestrator, orchestrator_brief, orchestrator_run,
                       personas, skills, web)
from test_orchestrator import DEFINITION, Model, OrchestratorCase, turn


def persona_body(**overrides) -> dict:
    return {"name": "Product Manager", "model": "openai/gpt-test", "guidelines": "Own the outcome.",
            **overrides}


# --- the persona record -------------------------------------------------------

class PersonaTests(OrchestratorCase):
    def test_a_name_and_a_model_are_required(self):
        for body in ({"model": "x/y"}, {"name": "PM"}, {"name": "PM", "model": "has space"},
                     {"name": "x" * 61, "model": "x/y"}):
            with self.assertRaises(ValueError):
                personas.load(body)

    def test_agent_models_are_validated_like_the_new_agent_sheet(self):
        with self.assertRaises(ValueError):
            personas.load(persona_body(agentDefaults={"model": "--dangerously-skip-permissions"}))
        with self.assertRaises(ValueError):
            personas.load(persona_body(allowedModels=["claude-opus-5", "not a model"]))

    def test_the_default_model_must_sit_inside_the_fence(self):
        with self.assertRaises(ValueError):
            personas.load(persona_body(agentDefaults={"model": "sonnet"},
                                       allowedModels=["claude-opus-5"]))
        # a fence with no default uses its first entry, never "whatever the CLI picks"
        loaded = personas.load(persona_body(allowedModels=["claude-opus-5", "haiku"]))
        self.assertEqual(loaded.agent_defaults.model, "claude-opus-5")

    def test_names_are_unique_across_the_bank(self):
        personas.save(personas.load(persona_body()))
        with self.assertRaises(ValueError):
            personas.save(personas.load(persona_body(name="product manager")))

    def test_saving_the_definition_never_drops_its_memory(self):
        persona = personas.save(personas.load(persona_body()))
        personas.remember(persona.id, "Keep prompts short.")
        edited = personas.load({**persona.to_dict(), "guidelines": "New guidelines.", "memory": []})
        personas.save(edited)
        self.assertEqual([m["text"] for m in personas.get(persona.id).memory], ["Keep prompts short."])

    def test_memory_is_deduplicated_bounded_and_removable(self):
        persona = personas.save(personas.load(persona_body()))
        first = personas.remember(persona.id, "Keep  prompts\nshort.")
        again = personas.remember(persona.id, "keep prompts short.")
        self.assertEqual(first["id"], again["id"])
        with mock.patch.object(personas, "MEMORY_LIMIT", 3):
            for n in range(5):
                personas.remember(persona.id, f"lesson {n}")
        self.assertEqual([m["text"] for m in personas.get(persona.id).memory],
                         ["lesson 2", "lesson 3", "lesson 4"])
        self.assertTrue(personas.forget(persona.id, personas.get(persona.id).memory[0]["id"]))
        self.assertFalse(personas.forget(persona.id, "nope"))

    def test_starters_are_offered_once_with_only_installed_skills(self):
        created = personas.seed_starters({"worktree-task"}, model="openai/gpt-5.6")
        self.assertEqual(created, ["Product Manager", "Engineering Lead"])
        lead = next(p for p in personas.list_all() if p.name == "Engineering Lead")
        self.assertEqual(lead.skills, ["worktree-task"])        # engineering-playbook is not installed
        self.assertEqual(lead.model, "openai/gpt-5.6")
        self.assertEqual(lead.agent_defaults.to_dict(),
                         {"engine": "claude", "model": "claude-opus-5", "mode": "interactive"})
        personas.delete(lead.id)
        self.assertEqual(personas.seed_starters({"worktree-task"}), [], "a deleted starter stays deleted")

    def test_a_starter_never_duplicates_a_persona_you_already_have(self):
        # setUp made "Tester"; add a Product Manager of your own first
        personas.save(personas.load(persona_body(guidelines="Mine.")))
        self.assertEqual(personas.seed_starters(set()), ["Engineering Lead"])
        mine = next(p for p in personas.list_all() if p.name == "Product Manager")
        self.assertEqual(mine.guidelines, "Mine.")


# --- the skills catalog -------------------------------------------------------

class SkillsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = mock.patch.object(skills, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, text):
        path = self.root / relative / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")

    def test_folded_and_plain_descriptions_are_both_read(self):
        self.write("worktree-task", "---\nname: worktree-task\ndescription: >-\n  Isolate every\n"
                                    "  new unit of work.\n---\nBody here.")
        self.write("plain", "---\nname: plain\ndescription: \"One line.\"\n---\n")
        found = {s["name"]: s["description"] for s in skills.list_skills()}
        self.assertEqual(found, {"plain": "One line.", "worktree-task": "Isolate every new unit of work."})

    def test_synced_skills_are_found_and_a_local_one_wins(self):
        self.write("synced/bucket-1/writing-playbook", "---\nname: writing-playbook\ndescription: synced\n---\n")
        self.write("synced/bucket-1/morning", "---\nname: morning\ndescription: synced\n---\n")
        self.write("writing-playbook", "---\nname: writing-playbook\ndescription: local\n---\n")
        found = {s["name"]: s["description"] for s in skills.list_skills()}
        self.assertEqual(found, {"morning": "synced", "writing-playbook": "local"})
        self.assertIn("name: writing-playbook", skills.read_skill("writing-playbook"))

    def test_an_unknown_name_is_refused_not_turned_into_a_path(self):
        with self.assertRaises(ValueError):
            skills.read_skill("../../etc/passwd")


# --- postings, their memory, and conversion from before personas -------------

class PostingTests(OrchestratorCase):
    def test_a_posting_remembers_facts_about_its_team(self):
        posting = self.make()
        item = orchestrator.remember(posting.id, "The SEO agent is retired.")
        self.assertEqual(orchestrator.remember(posting.id, "the seo agent is retired.")["id"], item["id"])
        self.assertEqual([m["text"] for m in orchestrator.memory(posting.id)], ["The SEO agent is retired."])
        self.assertTrue(orchestrator.forget(posting.id, item["id"]))
        self.assertEqual(orchestrator.memory(posting.id), [])

    def test_deleting_a_posting_takes_its_memory_with_it(self):
        posting = self.make()
        orchestrator.remember(posting.id, "fact")
        orchestrator.delete(posting.id)
        self.assertFalse((orchestrator.DIR / posting.id).exists())

    def test_an_older_orchestrator_becomes_a_persona_and_a_posting(self):
        legacy_id = "0123456789abcdef"
        home = orchestrator.DIR / legacy_id
        home.mkdir(parents=True)
        (home / "definition.json").write_text(json.dumps({
            "id": legacy_id, "name": "SEO Expert", "model": "openai/gpt-5.6-luna",
            "instructions": "You are an SEO lead.", "mode": "auto", "scope": "marketing"}))
        orchestrator.remember(legacy_id, "The domain is mlguerrilla.com.")
        (home / "journal.jsonl").write_text('{"at": 1, "type": "run_started"}\n')
        self.assertEqual(orchestrator.migrate_legacy(), ["SEO Expert"])
        posting = orchestrator.get(legacy_id)
        persona = personas.get(posting.persona_id)
        self.assertEqual((persona.name, persona.model, persona.guidelines),
                         ("SEO Expert", "openai/gpt-5.6-luna", "You are an SEO lead."))
        self.assertEqual(persona.agent_defaults.to_dict(),
                         {"engine": "claude", "model": "claude-opus-5", "mode": "interactive"})
        self.assertEqual((posting.mode, posting.scope, posting.model, posting.instructions),
                         ("auto", "marketing", "", ""))
        # its history and what it was told are exactly where they were
        self.assertEqual(orchestrator.memory(legacy_id)[0]["text"], "The domain is mlguerrilla.com.")
        self.assertEqual(orchestrator.journal(legacy_id)[0]["type"], "run_started")
        self.assertNotIn("model", json.loads((home / "definition.json").read_text()))
        self.assertEqual(orchestrator.migrate_legacy(), [], "converting twice changes nothing")


# --- a persona at work --------------------------------------------------------

class PersonaRunTests(OrchestratorCase):
    """The run obeys the persona -- and re-reads it every step."""

    def use(self, **fields):
        persona = personas.load({**self.persona.to_dict(), **fields})
        self.persona = personas.save(persona)
        return self.persona

    def start_calls(self, **args):
        return Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "Fix it", **args})]),
                     turn("", [("finish", {"summary": "ok"})]))

    def test_what_the_model_leaves_out_comes_from_the_persona(self):
        self.use(agentDefaults={"engine": "claude", "model": "claude-opus-5", "mode": "background"})
        self.run_with(self.start_calls(), mode="auto")
        started = self.host.started[0]
        self.assertEqual((started["engine"], started["model"], started["interactive"]),
                         ("claude", "claude-opus-5", False))

    def test_the_approval_banner_shows_what_will_really_run(self):
        self.use(agentDefaults={"engine": "claude", "model": "claude-opus-5", "mode": "interactive"})
        run = self.run_with(self.start_calls())                 # ask mode
        pending = run.snapshot()["pending"]
        self.assertEqual((pending["args"]["model"], pending["args"]["mode"], pending["args"]["engine"]),
                         ("claude-opus-5", "interactive", "claude"))

    def test_a_model_outside_the_fence_is_refused_before_anyone_is_asked(self):
        self.use(allowedModels=["claude-opus-5"])
        run = self.run_with(self.start_calls(model="claude-opus-4-5"))   # ask mode, still no banner
        self.assertIsNone(run.state["pending"])
        self.assertEqual(self.host.started, [])
        refusal = json.loads([m for m in run.state["messages"] if m.get("role") == "tool"][0]["content"])
        self.assertIn("not allowed", refusal["error"])
        self.assertIn("claude-opus-5", refusal["error"])

    def test_the_fence_holds_on_the_menu_too(self):
        self.use(allowedModels=["claude-opus-5"])
        specs = orchestrator_brief.tool_specs(self.context(orchestrator.load(self.body())))
        start = next(s for s in specs if s["function"]["name"] == "start_agent")
        self.assertIn("claude-opus-5", start["function"]["parameters"]["properties"]["model"]["description"])

    def test_a_saved_agent_starts_exactly_as_it_was_saved(self):
        self.host.saved = [{"name": "MLG-Lesson-Builder", "engine": "claude", "model": "opus",
                            "systemPrompt": "You build lessons." * 3}]
        self.use(team=["MLG-Lesson-Builder"], allowedModels=["claude-opus-5"])
        self.run_with(self.start_calls(savedAgent="MLG-Lesson-Builder", systemPrompt="Lesson 4 only."),
                      mode="auto")
        started = self.host.started[0]
        self.assertEqual((started["name"], started["model"]), ("MLG-Lesson-Builder", "opus"),
                         "your saved choice is not second-guessed by the fence")
        self.assertTrue(started["systemPrompt"].startswith("You build lessons."))
        self.assertTrue(started["systemPrompt"].endswith("Lesson 4 only."))

    def test_a_second_copy_of_a_saved_agent_gets_its_own_name(self):
        self.host.saved = [{"name": "Builder", "engine": "claude", "model": "", "systemPrompt": ""}]
        self.use(team=["Builder"])
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "a", "savedAgent": "Builder"}),
                                ("start_agent", {"project": "/tmp/project", "task": "b", "savedAgent": "Builder"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        self.run_with(model, mode="auto", limits={"maxConcurrent": 5})
        self.assertEqual([s["name"] for s in self.host.started], ["Builder", "Builder 2"])

    def test_only_its_own_team_can_be_started_as_saved_agents(self):
        self.host.saved = [{"name": "Other", "engine": "claude", "model": "", "systemPrompt": ""}]
        run = self.run_with(self.start_calls(savedAgent="Other"), mode="auto")
        self.assertEqual(self.host.started, [])
        refusal = json.loads([m for m in run.state["messages"] if m.get("role") == "tool"][0]["content"])
        self.assertIn("not one of your saved agents", refusal["error"])

    def test_a_persona_that_prefers_interactive_is_not_asked_about_it_in_auto(self):
        self.use(agentDefaults={"engine": "claude", "model": "claude-opus-5", "mode": "interactive"})
        run = self.run_with(self.start_calls(), mode="auto")
        self.assertEqual(run.state["status"], "done")
        self.assertTrue(self.host.started[0]["interactive"])

    def test_an_always_interactive_persona_starts_interactive_whatever_the_model_asks(self):
        """The bug that prompted the rule: Session said Interactive, the model
        asked for background, and got it. Now the persona's session wins."""
        self.use(agentDefaults={"engine": "claude", "model": "claude-opus-5", "mode": "interactive"})
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "research",
                                                 "mode": "background", "name": "M9"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        self.run_with(model, mode="auto")
        self.assertTrue(self.host.started[0]["interactive"])

    def test_an_always_background_persona_never_opens_a_window(self):
        self.use(agentDefaults={"engine": "claude", "model": "", "mode": "background"})
        run = self.run_with(self.start_calls(mode="interactive"), mode="auto")
        self.assertFalse(self.host.started[0]["interactive"])
        self.assertIsNone(run.state["pending"], "background never needs the terminal approval")

    def test_let_it_decide_leaves_the_choice_to_the_model(self):
        self.use(agentDefaults={"engine": "claude", "model": "", "mode": "either"})
        self.run_with(self.start_calls(mode="background"), mode="auto")
        self.assertFalse(self.host.started[0]["interactive"])
        self.host.started.clear()
        self.run_with(self.start_calls(), mode="auto")
        self.assertFalse(self.host.started[0]["interactive"], "saying nothing means background")

    def test_the_menu_only_offers_the_session_it_is_allowed(self):
        self.use(agentDefaults={"engine": "claude", "model": "", "mode": "interactive"})
        specs = orchestrator_brief.tool_specs(self.context(orchestrator.load(self.body())))
        start = next(s for s in specs if s["function"]["name"] == "start_agent")
        self.assertEqual(start["function"]["parameters"]["properties"]["mode"]["enum"], ["interactive"])
        self.assertIn("AgentGrid enforces it", start["function"]["description"])
        brief = orchestrator_brief.system_prompt(self.context(orchestrator.load(self.body())))
        self.assertIn("Every agent you start runs interactive", brief)

    def test_a_host_without_a_terminal_says_why_an_interactive_only_persona_cannot_start(self):
        self.use(agentDefaults={"engine": "claude", "model": "", "mode": "interactive"})
        self.host.caps = {**self.host.caps, "interactive": False}
        run = self.run_with(self.start_calls(), mode="auto")
        self.assertEqual(self.host.started, [])
        refusal = json.loads([m for m in run.state["messages"] if m.get("role") == "tool"][0]["content"])
        self.assertIn("only starts interactive agents", refusal["error"])

    def test_a_persona_saved_before_the_rule_lets_the_model_choose(self):
        loaded = personas.load({"name": "Old", "model": "x/y", "agentDefaults": {"engine": "claude"}})
        self.assertEqual(loaded.agent_defaults.mode, "either")
        self.assertEqual(loaded.agent_defaults.locked_mode(), "")

    def test_remember_puts_lessons_on_the_persona_and_facts_on_the_posting(self):
        model = Model(turn("", [("remember", {"text": "Keep prompts short.", "scope": "persona"}),
                                ("remember", {"text": "The SEO agent is retired.", "scope": "posting"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        run = self.run_with(model, mode="auto")
        self.assertEqual([m["text"] for m in personas.get(self.persona.id).memory], ["Keep prompts short."])
        self.assertEqual([m["text"] for m in orchestrator.memory(run.definition.id)],
                         ["The SEO agent is retired."])
        saved = [e for e in orchestrator.journal(run.definition.id) if e["type"] == "memory_saved"]
        self.assertEqual([(e["scope"], e["owner"]) for e in saved],
                         [("persona", "Tester"), ("posting", "Shipper")])
        # and the very next call already carries both
        brief = model.requests[1]["messages"][0]["content"]
        self.assertIn("Keep prompts short.", brief)
        self.assertIn("The SEO agent is retired.", brief)

    def test_a_lesson_travels_to_every_posting_of_the_persona(self):
        personas.remember(self.persona.id, "Default to interactive agents.")
        other = orchestrator.save(orchestrator.load(self.body(name="Other team", scope="design")))
        brief = orchestrator_brief.system_prompt(self.context(other))
        self.assertIn("Default to interactive agents.", brief)

    def test_only_its_own_skills_and_prompts_can_be_read(self):
        self.host.installed = [{"name": "worktree-task", "description": "Isolate work.", "body": "FULL SKILL"},
                               {"name": "sales-playbook", "description": "Sell.", "body": "nope"}]
        self.host.library = [{"name": "plan-tickets", "description": "Plan.", "body": "FULL PROMPT"}]
        self.use(skills=["worktree-task"], prompts=["plan-tickets"])
        model = Model(turn("", [("read_skill", {"name": "worktree-task"}),
                                ("read_skill", {"name": "sales-playbook"}),
                                ("read_prompt", {"name": "plan-tickets"})]),
                      turn("", [("finish", {"summary": "ok"})]))
        run = self.run_with(model, mode="auto")
        results = [json.loads(m["content"]) for m in run.state["messages"] if m.get("role") == "tool"]
        self.assertEqual(results[0]["text"], "FULL SKILL")
        self.assertIn("not one of your skills", results[1]["error"])
        self.assertEqual(results[2]["text"], "FULL PROMPT")
        tools = [t["function"]["name"] for t in model.requests[0]["tools"]]
        self.assertIn("read_skill", tools)
        self.assertIn("read_prompt", tools)

    def test_an_edit_in_the_bank_reaches_a_run_already_in_flight(self):
        model = Model(turn("Which repo?"), turn("", [("finish", {"summary": "ok"})]))
        run = self.run_with(model, mode="auto")
        self.use(guidelines="Always answer in French.")
        with mock.patch.object(openrouter, "tool_turn", model):
            run.submit("the second one")
            self.settle(run)
        self.assertIn("Always answer in French.", model.requests[1]["messages"][0]["content"])

    def test_start_again_keeps_its_agents_its_memory_and_how_it_ended(self):
        model = Model(turn("", [("start_agent", {"project": "/tmp/project", "task": "t", "name": "w"}),
                                ("remember", {"text": "Use the worktree.", "scope": "posting"})]),
                      turn("", [("finish", {"summary": "Shipped the parser."})]))
        run = self.run_with(model, mode="auto")
        again = Model(turn("", [("finish", {"summary": "ok"})]))
        with mock.patch.object(openrouter, "tool_turn", again):
            run.start("Next goal")
            self.settle(run)
        self.assertEqual([c["name"] for c in run.state["children"]], ["w"])
        self.assertEqual(run.state["spawns"], 0, "the per-run limit starts over")
        brief = again.requests[0]["messages"][0]["content"]
        self.assertIn("Use the worktree.", brief)
        self.assertIn("Shipped the parser.", brief)

    def test_a_deleted_persona_stops_the_run_with_a_reason(self):
        model = Model(turn("Which repo?"), turn("", [("finish", {"summary": "ok"})]))
        run = self.run_with(model, mode="auto")
        personas.delete(self.persona.id)
        with mock.patch.object(openrouter, "tool_turn", model):
            run.submit("go on")
            self.settle(run)
        self.assertEqual(run.state["status"], "failed")
        self.assertIn("persona", run.state["reason"])

    def test_the_brief_is_built_from_everything_it_has(self):
        self.host.saved = [{"name": "Slate-UI", "engine": "claude", "model": "opus",
                            "systemPrompt": "\nYou design Slate UI.\nMore."}]
        self.host.installed = [{"name": "worktree-task", "description": "Isolate work.", "body": ""}]
        self.host.library = [{"name": "i-have-adhd", "description": "Lead with the next action.", "body": ""}]
        self.use(team=["Slate-UI", "Gone-agent"], skills=["worktree-task", "missing-skill"],
                 prompts=["i-have-adhd"], guidelines="Own the outcome.")
        posting = orchestrator.save(orchestrator.load(self.body(brief="MLG is a course site.")))
        run = orchestrator_run.Run(posting, self.host)
        brief = orchestrator_brief.system_prompt(run._context())
        for expected in ("Own the outcome.", "Slate-UI (claude, opus): You design Slate UI.",
                         "worktree-task: Isolate work.", "i-have-adhd: Lead with the next action.",
                         "MLG is a course site."):
            self.assertIn(expected, brief)
        self.assertNotIn("Gone-agent", brief, "a removed saved agent is skipped, not fatal")
        self.assertNotIn("missing-skill", brief)


# --- the HTTP surface ---------------------------------------------------------

class PersonaRouteTests(OrchestratorCase):
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

    def test_the_bank_lists_postings_and_the_libraries(self):
        handler = self.handler()
        self.make()
        with mock.patch.object(web, "load_saved_agents", return_value=[
                {"name": "Slate-UI", "engine": "claude", "model": "opus", "systemPrompt": "\nDesign.\n"}]), \
                mock.patch.object(web, "load_saved_prompts", return_value=[
                    {"name": "plan-tickets", "description": "Plan.", "body": "x", "areaId": ""}]), \
                mock.patch.object(skills, "list_skills", return_value=[
                    {"name": "worktree-task", "description": "Isolate.", "path": "/x"}]):
            handler._personas()
        code, payload = self.last()
        self.assertEqual(code, 200)
        tester = next(p for p in payload["personas"] if p["name"] == "Tester")
        self.assertEqual([p["name"] for p in tester["postings"]], ["Shipper"])
        self.assertEqual(payload["catalog"]["agents"][0]["summary"], "Design.")
        self.assertEqual(payload["catalog"]["prompts"][0]["name"], "plan-tickets")
        self.assertEqual(payload["catalog"]["skills"][0]["name"], "worktree-task")

    def test_a_persona_cannot_be_deleted_while_it_is_posted(self):
        handler = self.handler()
        posting = self.make()
        handler._persona_action("delete", {"id": self.persona.id})
        self.assertEqual(self.last()[0], 400)
        self.assertIn("Shipper", self.last()[1]["error"])
        handler._orchestrator_action("delete", {"id": posting.id})
        handler._persona_action("delete", {"id": self.persona.id})
        self.assertEqual(self.last()[0], 200)

    def test_a_posting_needs_a_persona_that_exists(self):
        handler = self.handler()
        handler._orchestrator_action("save", {**DEFINITION, "personaId": "deadbeef"})
        self.assertEqual(self.last()[0], 400)
        self.assertEqual(orchestrator.list_all(), [])

    def test_undo_from_the_chat_removes_the_right_memory(self):
        handler = self.handler()
        posting = self.make()
        lesson = personas.remember(self.persona.id, "lesson")
        fact = orchestrator.remember(posting.id, "fact")
        handler._orchestrator_action("forget", {"id": posting.id, "scope": "persona", "memoryId": lesson["id"]})
        handler._orchestrator_action("forget", {"id": posting.id, "scope": "posting", "memoryId": fact["id"]})
        self.assertEqual(personas.get(self.persona.id).memory, [])
        self.assertEqual(orchestrator.memory(posting.id), [])
        handler._orchestrator_action("forget", {"id": posting.id, "scope": "posting", "memoryId": fact["id"]})
        self.assertEqual(self.last()[0], 400, "undoing twice says so")

    def test_the_snapshot_says_who_it_is_and_what_it_remembers(self):
        handler = self.handler()
        posting = self.make()
        personas.remember(self.persona.id, "lesson")
        orchestrator.remember(posting.id, "fact")
        handler._orchestrators()
        snap = self.last()[1]["orchestrators"][0]
        self.assertEqual((snap["persona"]["name"], snap["model"]), ("Tester", "openai/gpt-test"))
        self.assertEqual([m["text"] for m in snap["personaMemory"]], ["lesson"])
        self.assertEqual([m["text"] for m in snap["memory"]], ["fact"])

    def test_startup_converts_then_seeds_without_duplicates(self):
        legacy = orchestrator.DIR / "0123456789abcdef"
        legacy.mkdir(parents=True)
        (legacy / "definition.json").write_text(json.dumps(
            {"id": "0123456789abcdef", "name": "Product Manager", "model": "openai/gpt-5.6-luna"}))
        with mock.patch.object(skills, "list_skills", return_value=[]):
            converted, seeded = web.prepare_personas()
            self.assertEqual((converted, seeded), (["Product Manager"], ["Engineering Lead"]))
            self.assertEqual(web.prepare_personas(), ([], []))
        lead = next(p for p in personas.list_all() if p.name == "Engineering Lead")
        self.assertEqual(lead.model, "openai/gpt-5.6-luna", "starters think with the model you already use")


if __name__ == "__main__":
    unittest.main()
