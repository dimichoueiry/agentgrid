"""`ag doctor` and `ag hook install`, run as the real CLI under a throwaway HOME.

Both commands touch files the user owns, so every test gets its own HOME and a
PATH holding only stub binaries: nothing here can read or write the real
~/.claude/settings.json, and the result does not depend on what this machine
happens to have installed.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "hooks" / "agentgrid_notify.py"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class SetupCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "home"
        self.bin = Path(folder.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.settings = self.home / ".claude" / "settings.json"

    def stub(self, name: str, version: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\necho '{version}'\n")
        path.chmod(0o755)

    def ag(self, *args: str) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
               "PYTHONPATH": str(REPO),
               # Codex is also looked for in /Applications; point the override
               # at nothing so only the stub PATH decides.
               "AGENTGRID_CODEX_BIN": str(self.bin / "codex")}
        return subprocess.run([sys.executable, "-m", "agentgrid", *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def write_settings(self, text: str, mode: int = 0o644) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(text)
        self.settings.chmod(mode)

    def ours(self, data: dict, event: str) -> list[str]:
        return [h["command"] for group in data["hooks"][event] for h in group["hooks"]
                if "agentgrid_notify.py" in h["command"]]


class HookInstallTests(SetupCase):
    def test_creates_settings_owner_only_when_absent(self):
        result = self.ag("hook", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("added Notification, added UserPromptSubmit", result.stdout)
        data = json.loads(self.settings.read_text())
        for event in ("Notification", "UserPromptSubmit"):
            self.assertEqual(self.ours(data, event), [f"python3 {SCRIPT}"])
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)

    def test_preserves_unrelated_settings_indent_and_mode(self):
        original = {
            "model": "opus",
            "env": {"SOME_TOKEN": "secret-value"},
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"Notification": [{"matcher": "", "hooks": [
                {"type": "command", "command": "say done"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]},
        }
        self.write_settings(json.dumps(original, indent=4) + "\n", mode=0o640)
        self.assertEqual(self.ag("hook", "install").returncode, 0)

        text = self.settings.read_text()
        data = json.loads(text)
        self.assertEqual(list(data), ["model", "env", "permissions", "hooks"])
        self.assertEqual(data["env"], original["env"])
        self.assertEqual(data["permissions"], original["permissions"])
        self.assertEqual(data["hooks"]["Stop"], original["hooks"]["Stop"])
        self.assertEqual(data["hooks"]["Notification"][0], original["hooks"]["Notification"][0])
        self.assertEqual(len(self.ours(data, "Notification")), 1)
        self.assertTrue(text.startswith('{\n    "model"'), "indent should stay at 4")
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o640)
        self.assertEqual([p.name for p in self.settings.parent.iterdir()], ["settings.json"],
                         "no temp file left behind")

    def test_second_install_is_a_noop_that_leaves_the_file_alone(self):
        self.assertEqual(self.ag("hook", "install").returncode, 0)
        before, mtime = self.settings.read_bytes(), self.settings.stat().st_mtime_ns
        result = self.ag("hook", "install")
        self.assertEqual(result.returncode, 0)
        self.assertIn("already installed", result.stdout)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.settings.stat().st_mtime_ns, mtime)

    def test_repoints_a_stale_checkout_instead_of_duplicating(self):
        stale = "python3 /old/clone/hooks/agentgrid_notify.py"
        self.write_settings(json.dumps({"hooks": {
            "Notification": [{"hooks": [{"type": "command", "command": stale}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": stale}]}]}}))
        result = self.ag("hook", "install")
        self.assertIn("updated Notification, updated UserPromptSubmit", result.stdout)
        data = json.loads(self.settings.read_text())
        for event in ("Notification", "UserPromptSubmit"):
            self.assertEqual(self.ours(data, event), [f"python3 {SCRIPT}"])

    def test_refuses_invalid_json_and_leaves_it_untouched(self):
        self.write_settings('{"model": "opus",,}')
        result = self.ag("hook", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not valid JSON (line 1", result.stderr)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertEqual(self.settings.read_text(), '{"model": "opus",,}')

    def test_refuses_unexpected_hooks_shape(self):
        self.write_settings('{"hooks": []}')
        result = self.ag("hook", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn('"hooks" in settings.json is not an object', result.stderr)
        self.assertEqual(self.settings.read_text(), '{"hooks": []}')

    def test_writes_through_a_symlinked_settings_file(self):
        real = self.home / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        real.write_text('{"model": "opus"}\n')
        self.settings.parent.mkdir()
        self.settings.symlink_to(real)
        self.assertEqual(self.ag("hook", "install").returncode, 0)
        self.assertTrue(self.settings.is_symlink())
        data = json.loads(real.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(len(self.ours(data, "UserPromptSubmit")), 1)


class DoctorTests(SetupCase):
    def doctor(self) -> subprocess.CompletedProcess:
        return self.ag("doctor", "--port", str(_free_port()))

    def test_healthy_machine_reports_every_check(self):
        self.stub("claude", "2.1.0 (Claude Code)")
        self.stub("codex", "codex-cli 0.9.0")
        self.ag("hook", "install")
        result = self.doctor()
        self.assertEqual(result.returncode, 0, result.stdout)
        out = result.stdout
        self.assertIn("ok    Python ", out)
        self.assertIn("ok    claude 2.1.0 (Claude Code)", out)
        self.assertIn("ok    codex codex-cli 0.9.0", out)
        self.assertIn("ok    state dir ~/.agentgrid not created yet", out)
        self.assertIn("is free for `ag --web`", out)
        self.assertIn("ok    hook installed for Notification, UserPromptSubmit", out)
        self.assertIn("0 problems, 0 warnings.", out)

    def test_missing_codex_and_hook_are_warnings_with_a_fix(self):
        self.stub("claude", "2.1.0 (Claude Code)")
        result = self.doctor()
        self.assertEqual(result.returncode, 0)
        self.assertIn("warn  codex not found", result.stdout)
        self.assertIn("run `ag hook install`", result.stdout)

    def test_no_engine_at_all_is_a_failure(self):
        result = self.doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  neither claude nor codex is installed", result.stdout)

    def test_unwritable_state_dir_fails(self):
        self.stub("claude", "2.1.0")
        state = self.home / ".agentgrid"
        state.mkdir(mode=0o500)
        self.addCleanup(state.chmod, 0o700)
        result = self.doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  state dir ~/.agentgrid is not writable", result.stdout)

    def test_stale_hook_path_and_partial_install_are_flagged(self):
        self.stub("claude", "2.1.0")
        self.write_settings(json.dumps({"hooks": {"Notification": [{"hooks": [
            {"type": "command", "command": "python3 /gone/hooks/agentgrid_notify.py"}]}]}}))
        out = self.doctor().stdout
        self.assertIn("hook missing for UserPromptSubmit", out)
        self.assertIn("script that no longer exists", out)

    def test_never_prints_secrets(self):
        self.stub("claude", "2.1.0")
        self.write_settings(json.dumps({"env": {"API_KEY": "sk-settings-secret"}}))
        record = self.home / ".agentgrid" / "web.json"
        record.parent.mkdir()
        record.write_text(json.dumps({"url": "http://127.0.0.1:9", "port": 9,
                                      "token": "board-token-secret", "pid": 999999}))
        record.chmod(0o644)
        out = self.doctor().stdout
        self.assertNotIn("sk-settings-secret", out)
        self.assertNotIn("board-token-secret", out)
        self.assertIn("readable by others (mode 644)", out)
        self.assertIn("not answering", out)


if __name__ == "__main__":
    unittest.main()
