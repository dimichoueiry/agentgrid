"""Work area persistence, membership, and deferred launch association."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agentgrid import areas, web


class AreaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(areas, 'PATH', Path(tmp.name) / 'areas.json')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(areas, '_PENDING', [])
        p.start()
        self.addCleanup(p.stop)

    def test_create_edit_assign_delete_preserves_other_areas(self):
        data = areas.update({'action': 'save', 'name': 'Research', 'prompt': 'Find sources'})
        created = data['areas'][-1]['id']
        areas.update({'action': 'assign', 'id': created, 'sessionId': 'agent-1'})
        areas.update({'action': 'assign', 'id': 'marketing', 'sessionId': 'agent-2'})
        areas.update({'action': 'save', 'id': created, 'name': 'Insights', 'prompt': 'Analyze'})
        self.assertEqual(areas.load()['members']['agent-1'], created)
        self.assertEqual(areas.load()['areas'][-1]['prompt'], 'Analyze')
        areas.update({'action': 'delete', 'id': created})
        self.assertEqual(areas.load()['members'], {'agent-2': 'marketing'})

    def test_invalid_edits_do_not_overwrite(self):
        original = areas.load()
        for body in ({'action': 'save', 'name': ''}, {'action': 'save', 'name': 'MARKETING'},
                     {'action': 'assign', 'id': 'missing', 'sessionId': 's'},
                     {'action': 'save', 'id': 'missing', 'name': 'New'}):
            with self.assertRaises(ValueError):
                areas.update(body)
        self.assertEqual(areas.load(), original)

    def session(self, sid='new', **kwargs):
        fields = dict(session_id=sid, cwd='/tmp/project', engine='codex', job_id=None,
                      started_at=int(time.time() * 1000), kind='interactive')
        fields.update(kwargs)
        return SimpleNamespace(**fields)

    def test_launch_ignores_existing_and_wrong_engine_and_resolves_once(self):
        areas.await_session('marketing', '/tmp/project', 'codex', True, None, ['old'], time.time())
        areas.resolve([self.session('old'), self.session('wrong', engine='claude')])
        self.assertEqual(areas.load()['members'], {})
        areas.resolve([self.session(), self.session('later')])
        self.assertEqual(areas.load()['members'], {'new': 'marketing'})
        self.assertEqual(areas._PENDING, [])

    def test_job_id_and_expired_launch(self):
        areas.await_session('design', '/tmp/project', 'claude', False, 'job', [], time.time())
        areas.resolve([self.session('wrong'), self.session(job_id='job', engine='claude')])
        self.assertEqual(areas.load()['members'], {'new': 'design'})
        areas.await_session('design', '/tmp/project', 'codex', True, None, [], time.time() - 301)
        areas.resolve([self.session('later')])
        self.assertNotIn('later', areas.load()['members'])

    def test_spawn_registers_selected_area_and_rejects_missing_area(self):
        handler = object.__new__(web.Handler)
        handler.fleet = SimpleNamespace(raw=lambda: [])
        replies = []
        handler._send_json = lambda code, data: replies.append((code, data))
        with patch.object(web, 'discover_projects', return_value=[{'path': '/tmp/project'}]), \
                patch.object(web, 'spawn_agent', return_value=(True, 'Started', 'job')) as spawn:
            handler._spawn({'cwd': '/tmp/project', 'prompt': 'test', 'areaId': 'marketing'})
            self.assertEqual(replies[-1][0], 200)
            self.assertEqual(areas._PENDING[-1]['area'], 'marketing')
            handler._spawn({'cwd': '/tmp/project', 'prompt': 'test', 'areaId': 'missing'})
            self.assertEqual(replies[-1][0], 400)
            self.assertEqual(spawn.call_count, 1)

    def test_unassign(self):
        areas.update({'action': 'assign', 'id': 'engineering', 'sessionId': 's'})
        areas.update({'action': 'assign', 'id': '', 'sessionId': 's'})
        self.assertEqual(areas.load()['members'], {})


if __name__ == '__main__':
    unittest.main()
