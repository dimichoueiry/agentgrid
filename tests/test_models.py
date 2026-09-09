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
                    mock.patch.object(web.subprocess, 'Popen') as popen, \
                    mock.patch.object(web.subprocess, 'run') as run:
                run.return_value = mock.Mock(returncode=0, stdout='Started abcdef12', stderr='')
                ok, _, _ = web.spawn_agent('/tmp/repo', 'hello', 'exact-model-v123',
                                          [{'path': '/tmp/repo'}], engine=engine)
                self.assertTrue(ok)
                argv = (popen if engine == 'codex' else run).call_args.args[0]
                self.assertEqual(argv[argv.index('-m' if engine == 'codex' else '--model') + 1], 'exact-model-v123')
