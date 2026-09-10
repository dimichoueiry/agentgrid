import unittest
from unittest import mock
from agentgrid import executables


class ExecutableTests(unittest.TestCase):
    def tearDown(self):
        executables.codex_binary.cache_clear()

    def test_newest_installed_cli_wins(self):
        with mock.patch.dict(executables.os.environ, {}, clear=True), \
             mock.patch.object(executables.shutil, "which", return_value="/npm/codex"), \
             mock.patch.object(executables.Path, "is_file", return_value=True), \
             mock.patch.object(executables.os, "access", return_value=True), \
             mock.patch.object(executables, "_version", side_effect=lambda p: (0,146,0) if p == "/npm/codex" else (0,153,4)):
            self.assertEqual(executables.codex_binary(), "/Applications/Codex.app/Contents/Resources/codex")

    def test_explicit_override_is_respected(self):
        with mock.patch.dict(executables.os.environ, {"AGENTGRID_CODEX_BIN": "/custom/codex"}):
            self.assertEqual(executables.codex_binary(), "/custom/codex")

    def test_missing_installation_falls_back(self):
        with mock.patch.dict(executables.os.environ, {}, clear=True), \
             mock.patch.object(executables.shutil, "which", return_value=None), \
             mock.patch.object(executables.Path, "is_file", return_value=False):
            self.assertEqual(executables.codex_binary(), "codex")
