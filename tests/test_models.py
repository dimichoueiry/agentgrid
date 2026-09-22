import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentgrid import models, web


class ModelCatalogTests(unittest.TestCase):
    def test_missing_and_malformed_caches_keep_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
            root = Path(tmp)
            (root / 'models_cache.json').write_text('{broken')
            (root / '.claude.json').write_text('{"projects": []}')
            result = models.catalog(home=root)
            self.assertEqual(result['codex'], [["", "Default"]])
            self.assertIn(['opus', 'Opus (alias)'], result['claude'])

    def test_local_models_are_filtered_deduplicated_and_engine_specific(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
            root = Path(tmp)
            (root / 'models_cache.json').write_text(json.dumps({'models': [
                {'slug': 'codex-exact', 'display_name': 'Exact', 'visibility': 'list'},
                {'slug': 'internal', 'visibility': 'hide'}, None]}))
            (root / '.claude.json').write_text(json.dumps({'projects': {'/repo': {
                'lastModelUsage': {'claude-exact-version': {}, 'other': {}}}}}))
            result = models.catalog([
                {'engine': 'codex', 'model': 'codex-exact'},
                {'engine': 'claude', 'model': 'claude-exact-version'}], home=root)
            self.assertEqual(result['codex'], [["", "Default"], ['codex-exact', 'Exact']])
            self.assertEqual(sum(v == 'claude-exact-version' for v, _ in result['claude']), 1)
            self.assertNotIn('other', [v for v, _ in result['claude']])

    def test_exact_models_reach_launch_arguments(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine), \
                    mock.patch.object(models, 'listed', return_value=True), \
                    mock.patch.object(web.subprocess, 'Popen') as popen, \
                    mock.patch.object(web.subprocess, 'run') as run:
                run.return_value = mock.Mock(returncode=0, stdout='Started abcdef12', stderr='')
                ok, _, _ = web.spawn_agent('/tmp/repo', 'hello', 'exact-model-v123',
                                          [{'path': '/tmp/repo'}], engine=engine)
                self.assertTrue(ok)
                argv = (popen if engine == 'codex' else run).call_args.args[0]
                self.assertEqual(argv[argv.index('-m' if engine == 'codex' else '--model') + 1], 'exact-model-v123')

    def test_claude_ids_read_as_names(self):
        self.assertEqual(models.label('claude', 'claude-opus-5'), 'Claude Opus 5')
        self.assertEqual(models.label('claude', 'claude-fable-5-1'), 'Claude Fable 5.1')
        self.assertEqual(models.label('claude', 'claude-haiku-4-5-20251001'), 'Claude Haiku 4.5 (2025-10-01)')
        self.assertEqual(models.label('claude', 'claude-fable-5-1[1m]'), 'Claude Fable 5.1 (1M context)')
        self.assertEqual(models.label('claude', 'fable[1m]'), 'Fable (alias, 1M context)')
        self.assertEqual(models.label('claude', 'my-proxy-model'), 'my-proxy-model')
        self.assertEqual(models.label('codex', 'gpt-5.5'), 'gpt-5.5')

    def test_placeholders_and_hidden_models_are_not_offered(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
            root = Path(tmp)
            (root / 'models_cache.json').write_text(json.dumps({'models': [
                {'slug': 'gpt-5.5', 'display_name': 'GPT-5.5', 'visibility': 'list'},
                {'slug': 'codex-auto-review', 'visibility': 'hide'}]}))
            result = models.catalog([
                {'engine': 'claude', 'model': '<synthetic>'},
                {'engine': 'codex', 'model': 'codex-auto-review'},
                {'engine': 'codex', 'model': 'codex'},
                {'engine': 'claude', 'model': '--flag'}], home=root)
            self.assertEqual(result['codex'], [["", "Default"], ['gpt-5.5', 'GPT-5.5']])
            ids = [v for v, _ in result['claude']]
            self.assertNotIn('<synthetic>', ids)
            self.assertNotIn('--flag', ids)

    def test_a_model_a_session_used_stays_listed_after_it_ends(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
            root = Path(tmp)
            models.catalog([{'engine': 'claude', 'model': 'claude-opus-5'},
                            {'engine': 'codex', 'model': 'gpt-6-astra'}], home=root)
            later = models.catalog(home=root)
            self.assertIn(['claude-opus-5', 'Claude Opus 5'], later['claude'])
            self.assertIn(['gpt-6-astra', 'gpt-6-astra'], later['codex'])
            self.assertTrue(models.listed('claude', 'claude-opus-5', home=root))
            self.assertFalse(models.listed('codex', 'claude-opus-5', home=root))

    def test_aliases_first_then_newest_models(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
            root = Path(tmp)
            (root / '.claude').mkdir()
            (root / '.claude' / 'settings.json').write_text('{"model": "fable[1m]"}')
            (root / '.claude.json').write_text(json.dumps({
                'additionalModelOptionsCache': [{'value': 'claude-fable-5-1[1m]', 'label': 'Fable'}],
                'projects': {'/a': {'lastModelUsage': {'claude-opus-4-8': {}, 'claude-haiku-4-5-20251001': {}}}}}))
            ids = [v for v, _ in models.catalog([{'model': 'claude-opus-5'}], home=root)['claude']]
            self.assertEqual(ids, ['', 'fable', 'opus', 'sonnet', 'haiku', 'fable[1m]',
                                   'claude-fable-5-1[1m]', 'claude-opus-5', 'claude-opus-4-8',
                                   'claude-haiku-4-5-20251001'])


class LaunchCheckTests(unittest.TestCase):
    """A model this machine has no record of is checked at launch."""

    ALLOWED = [{'path': '/tmp/repo'}]

    def test_something_that_is_not_a_model_id_starts_nothing(self):
        with mock.patch.object(web.subprocess, 'run') as run, \
                mock.patch.object(web.subprocess, 'Popen') as popen:
            ok, message, _ = web.spawn_agent('/tmp/repo', 'hi', '--dangerously-skip-permissions', self.ALLOWED)
        self.assertFalse(ok)
        self.assertIn('not a model ID', message)
        run.assert_not_called()
        popen.assert_not_called()

    def test_a_listed_model_is_not_checked(self):
        with mock.patch.object(models, 'listed', return_value=True), \
                mock.patch.object(web.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='backgrounded · abcdef12', stderr='')
            ok, message, job = web.spawn_agent('/tmp/repo', 'hi', 'claude-opus-5', self.ALLOWED)
        self.assertEqual((ok, message, job), (True, 'Started in repo on claude-opus-5.', 'abcdef12'))
        self.assertEqual(run.call_count, 1)

    def test_an_interactive_session_is_not_checked_but_named(self):
        with mock.patch.object(models, 'listed') as listed, \
                mock.patch.object(web, '_open_terminal_tab', return_value=(True, 'ok')) as tab, \
                mock.patch.object(web.sys, 'platform', 'darwin'):
            ok, message, _ = web.spawn_agent('/tmp/repo', 'hi', 'claude-opus-9', self.ALLOWED, interactive=True)
        self.assertEqual((ok, message), (True, 'Opened an interactive claude in repo on claude-opus-9.'))
        self.assertIn('--model claude-opus-9', tab.call_args.args[0])
        listed.assert_not_called()

    def _claude(self, screen):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ['claude', 'logs']:
                return mock.Mock(returncode=0, stdout=screen, stderr='')
            return mock.Mock(returncode=0, stdout='backgrounded · d13d8296\n', stderr='')

        with mock.patch.object(models, 'listed', return_value=False), \
                mock.patch.object(web, 'MODEL_CHECK_SECONDS', 0), \
                mock.patch.object(web.subprocess, 'run', fake_run):
            result = web.spawn_agent('/tmp/repo', 'hi', 'claude-opus-9', self.ALLOWED)
        return result, calls

    def test_claude_refusing_the_model_is_reported_and_the_session_removed(self):
        # As `claude logs` prints it: a screen of cursor moves around the reply.
        screen = ('\x1b[2J\x1b[H\x1b[8B⏺\x1b[3GThere\'s an issue with the selected model '
                  '(claude-opus-9).\x1b[1CIt may not exist or you may not have access to it.\x1b[50;1H')
        (ok, message, job), calls = self._claude(screen)
        self.assertFalse(ok)
        self.assertIsNone(job)
        self.assertIn('did not accept the model claude-opus-9', message)
        self.assertIn(['claude', 'rm', 'd13d8296'], calls)

    def test_a_working_unknown_claude_model_starts(self):
        (ok, message, job), calls = self._claude('\x1b[2J❯ hi\x1b[1B✻ Thinking…')
        self.assertEqual((ok, message, job), (True, 'Started in repo on claude-opus-9.', 'd13d8296'))
        self.assertEqual([c[:2] for c in calls], [['claude', '--bg'], ['claude', 'logs']])

    def test_the_prompt_echoed_on_screen_is_not_a_refusal(self):
        (ok, _, _), _ = self._claude('❯ explain "There\'s an issue with the selected model"')
        self.assertTrue(ok)

    def _codex(self, exit_code, stderr=b''):
        def fake_popen(argv, **kwargs):
            if kwargs['stderr'] is not web.subprocess.DEVNULL:
                kwargs['stderr'].write(stderr)
                kwargs['stderr'].flush()
            return mock.Mock(poll=mock.Mock(return_value=exit_code))

        with mock.patch.object(models, 'listed', return_value=False), \
                mock.patch.object(web.chat, 'codex_binary', lambda: 'codex'), \
                mock.patch.object(web.subprocess, 'Popen', side_effect=fake_popen) as popen:
            return web.spawn_agent('/tmp/repo', 'hi', 'gpt-bogus', self.ALLOWED, engine='codex'), popen

    def test_codex_refusing_the_model_is_reported(self):
        err = (b'warning: Model metadata for `gpt-bogus` not found.\n'
               b'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
               b'"message":"The \'gpt-bogus\' model is not supported when using Codex with a ChatGPT account."}}\n')
        (ok, message, _), _ = self._codex(1, err)
        self.assertFalse(ok)
        self.assertEqual(message, "codex stopped straight away: The 'gpt-bogus' model is not supported "
                                  "when using Codex with a ChatGPT account.")

    def test_a_codex_run_still_going_after_the_check_started(self):
        with mock.patch.object(web, 'MODEL_CHECK_SECONDS', 0):
            (ok, message, _), popen = self._codex(None)
        self.assertEqual((ok, message), (True, 'Started codex in repo on gpt-bogus.'))
        self.assertEqual(popen.call_args.args[0][-3:], ['gpt-bogus', '--', 'hi'])

    def test_codex_error_falls_back_to_the_last_line(self):
        self.assertEqual(web._codex_error('starting\nnot logged in\n'), 'not logged in')
        self.assertEqual(web._codex_error('ERROR: plain words'), 'plain words')
        self.assertEqual(web._codex_error(''), 'it printed nothing.')
