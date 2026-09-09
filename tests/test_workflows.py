"""Workflow authoring, import and mixed-engine execution contracts."""
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from agentgrid import teams, workflows, web
from test_teams import FakeSession, _run


PIPELINE = {'name': 'Lesson', 'cwd': '/tmp', 'nodes': [
    {'id': 'plan', 'role': 'Architect', 'engine': 'claude', 'model': 'exact-claude',
     'instructions': 'Plan before writing.', 'prompt': 'Make a plan.', 'includeContext': True},
    {'id': 'write', 'role': 'Writer', 'engine': 'codex', 'model': 'exact-codex',
     'instructions': 'Teach mechanisms.', 'prompt': 'Write the lesson.', 'includeContext': True},
], 'loops': []}


class AuthoringTests(unittest.TestCase):
    def test_roundtrip_preserves_engine_instructions_context_and_models(self):
        team = teams.load_team(PIPELINE)
        again = teams.load_team(teams.team_to_dict(team))
        self.assertEqual(team, again)
        self.assertEqual(again.nodes[1].engine, 'codex')
        self.assertTrue(again.nodes[1].include_context)

    def test_import_fenced_workflow_without_saving_or_running(self):
        with mock.patch.object(teams, 'save_team') as save, mock.patch.object(teams.TeamRun, 'start') as start:
            got = workflows.parse_workflow('Here is your workflow:\n```json\n' + json.dumps(PIPELINE) + '\n```')
        self.assertEqual(got.name, 'Lesson')
        save.assert_not_called(); start.assert_not_called()

    def test_invalid_import_reports_duplicate_step(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            workflows.parse_workflow(json.dumps({**PIPELINE, 'nodes': [PIPELINE['nodes'][0]] * 2}))

    def test_reserved_ids_invalid_engines_and_forward_loops_are_rejected(self):
        for change in ({'id': 'input'}, {'id': 'with space'}, {'engine': 'other'}, {'includeContext': 'false'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                teams.load_team({**PIPELINE, 'nodes': [{**PIPELINE['nodes'][0], **change}]})
        with self.assertRaisesRegex(ValueError, 'earlier'):
            teams.load_team({**PIPELINE, 'loops': [{'at': 'plan', 'back_to': 'write', 'when': 'X'}]})

    def test_lesson_template_has_all_six_full_instruction_sets(self):
        path = Path(__file__).resolve().parents[1] / 'examples/teams/mlguerrilla-lessons.json'
        team = teams.load_team(json.loads(path.read_text()))
        self.assertEqual(len(team.nodes), 6)
        self.assertTrue(all(n.include_context and len(n.instructions) > 500 for n in team.nodes))
        self.assertEqual(team.loops[0].when, 'REQUIRES REVISION')

    def test_mixed_engine_handoff_includes_brief_and_full_output(self):
        prompts, engines = [], []
        class StreamSession(FakeSession):
            def __init__(self, sid, cwd, engine):
                super().__init__(sid, cwd, engine); engines.append(engine)
            def send(self, prompt, posture, model=''):
                prompts.append((prompt, model))
                self._emit({'type': 'assistant_message', 'text': 'Complete result ' + 'x' * 1200})
                self._emit({'type': 'turn_done', 'ok': True}) # Codex completion has no result field.
        with mock.patch.object(teams.chat, 'ChatSession', StreamSession):
            run, events = _run(teams.load_team(PIPELINE), 'Original lesson brief')
        self.assertTrue(run.ok)
        self.assertEqual(engines, ['claude', 'codex'])
        self.assertIn('Original lesson brief', prompts[1][0])
        self.assertIn('Plan before writing.', prompts[0][0])
        self.assertIn('Teach mechanisms.', prompts[1][0])
        self.assertIn('x' * 1200, prompts[1][0])
        self.assertEqual(prompts[1][1], 'exact-codex')
        self.assertGreater(len(run.outputs['write']), 1200)

    def test_same_workflow_cannot_run_twice_concurrently(self):
        manager = teams.TeamManager()
        with mock.patch.object(teams.TeamRun, 'start'):
            manager.start(teams.load_team(PIPELINE), 'brief')
            with self.assertRaisesRegex(ValueError, 'already running'):
                manager.start(teams.load_team(PIPELINE), 'brief')

    def test_generated_draft_is_separate_read_only_and_unsaved(self):
        captured = []
        class DraftSession(FakeSession):
            def send(self, prompt, posture, model=''):
                captured.append((self.session_id, self.engine, posture, model, prompt))
                self._emit({'type': 'assistant_message', 'text': json.dumps(PIPELINE)})
                self._emit({'type': 'turn_done', 'ok': True})
        with mock.patch.object(workflows.chat, 'ChatSession', DraftSession), \
                mock.patch.object(teams, 'save_team') as save:
            manager = workflows.DraftManager()
            job = manager.start('Architect then writer.', '/tmp', 'codex', 'chosen')
            deadline = time.monotonic() + 2
            while manager.status(job)['status'] == 'building' and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(manager.status(job)['status'], 'ready')
        self.assertEqual(captured[0][:4], (None, 'codex', 'read-only', 'chosen'))
        save.assert_not_called()
        self.assertNotIn('room', manager.status(job))

    def test_import_endpoint_does_not_use_model_or_persist(self):
        handler = object.__new__(web.Handler)
        handler.drafts = mock.Mock(); handler._send_json = mock.Mock()
        handler._workflow_draft({'source': json.dumps(PIPELINE), 'cwd': '/tmp'})
        handler.drafts.start.assert_not_called()
        self.assertEqual(handler._send_json.call_args.args[0], 200)
