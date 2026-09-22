"""AG2-3: owner-only state, `--` before every prompt, strict Content-Length.

Each group attacks one boundary the way a hostile input would reach it: a
prompt shaped like a CLI flag, a request whose framing lies, a state folder a
previous version left world-readable.
"""
from __future__ import annotations

import io
import os
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import __main__ as entry
from agentgrid import chat, models, state, web
from support import isolate_chat_queue

ROOT = Path(__file__).resolve().parent.parent

# Prompts a user, a voice command or an orchestrator's model could send, each
# of which the CLI would take as a flag if it were not behind `--`.
HOSTILE_PROMPTS = [
    "--version",
    "--dangerously-skip-permissions",
    "--permission-mode=bypassPermissions",
    "--add-dir=/",
    "-p",
    "-",
    "--",
    "-c model_provider=evil do the task",
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class StateDirTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.path = self.home / ".agentgrid"

    def test_a_new_folder_is_created_owner_only(self):
        old = os.umask(0o022)
        try:
            state.secure_state_dir(self.path)
        finally:
            os.umask(old)
        self.assertEqual(_mode(self.path), 0o700)

    def test_an_existing_world_readable_folder_is_tightened_and_its_contents_kept(self):
        self.path.mkdir(mode=0o755)
        os.chmod(self.path, 0o755)
        (self.path / "web.json").write_text('{"token": "x"}')
        (self.path / "notes").mkdir()
        state.secure_state_dir(self.path)
        self.assertEqual(_mode(self.path), 0o700)
        self.assertEqual((self.path / "web.json").read_text(), '{"token": "x"}')
        self.assertTrue((self.path / "notes").is_dir())

    def test_group_and_other_bits_are_all_cleared(self):
        self.path.mkdir()
        os.chmod(self.path, 0o777)
        state.secure_state_dir(self.path)
        self.assertEqual(_mode(self.path), 0o700)

    def test_an_already_private_folder_is_left_as_it_is(self):
        self.path.mkdir()
        os.chmod(self.path, 0o700)
        with mock.patch.object(state.os, "chmod") as chmod:
            state.secure_state_dir(self.path)
        chmod.assert_not_called()

    def test_a_file_in_the_way_is_not_fatal_and_not_changed(self):
        self.path.write_text("not a folder")
        os.chmod(self.path, 0o644)
        state.secure_state_dir(self.path)  # must not raise
        self.assertEqual(_mode(self.path), 0o644)

    def test_a_folder_owned_by_someone_else_is_left_alone(self):
        self.path.mkdir()
        os.chmod(self.path, 0o755)
        with mock.patch.object(state.os, "getuid", return_value=os.getuid() + 1), \
                mock.patch.object(state.os, "chmod") as chmod:
            state.secure_state_dir(self.path)
        chmod.assert_not_called()

    def test_the_default_path_follows_home_at_call_time(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(state.secure_state_dir(), self.path)
        self.assertEqual(_mode(self.path), 0o700)

    def test_every_entry_point_secures_it_first(self):
        # grid, web and ticket all come through main(); the ticket verb returns
        # before argparse, so it is the one most easily missed.
        for argv, target in ((["ticket", "list"], "agentgrid.ticket_cli.main"),
                             (["--web", "--no-open"], "agentgrid.web.serve"),
                             ([], "agentgrid.ui.main")):
            with self.subTest(argv=argv), \
                    mock.patch.object(state, "secure_state_dir") as secure, \
                    mock.patch(target, return_value=0):
                try:
                    entry.main(argv)
                except SystemExit:
                    pass
                secure.assert_called_once_with()

    def test_the_notify_hook_creates_the_folder_owner_only(self):
        env = {**os.environ, "HOME": str(self.home)}
        payload = '{"session_id": "s1", "hook_event_name": "UserPromptSubmit"}'
        done = subprocess.run([sys.executable, str(ROOT / "hooks" / "agentgrid_notify.py")],
                              input=payload, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(done.returncode, 0)
        self.assertTrue(self.path.is_dir())
        self.assertEqual(_mode(self.path), 0o700)


class PromptIsNeverAFlagTests(unittest.TestCase):
    """Every path that hands a prompt to a CLI ends its argv with `--`, prompt."""

    ALLOWED = [{"path": "/tmp/repo"}]

    def setUp(self):
        isolate_chat_queue(self)

    def assertEndsWithPrompt(self, argv, prompt):
        self.assertEqual(argv[-2:], ["--", prompt], argv)
        # Nothing between `--` and the prompt, and no second `--` earlier that
        # could leave a later option parsed as a flag.
        self.assertEqual(argv.index("--"), len(argv) - 2, argv)

    def test_chat_claude_turn(self):
        room = chat.ChatSession("sid", "/repo", "claude")
        proc = mock.Mock(stdout=io.StringIO(""), returncode=0)
        for prompt in HOSTILE_PROMPTS:
            with self.subTest(prompt=prompt), \
                    mock.patch.object(chat.subprocess, "Popen", return_value=proc) as popen:
                # With attachments too: --add-dir is variadic and would
                # swallow an unguarded positional after it.
                room._run_turn(prompt, "read-only", "", ["/u/s1/a.pdf"])
                argv = popen.call_args.args[0]
                self.assertEqual(argv[:2], [chat.CLAUDE_BIN, "-p"])
                self.assertEndsWithPrompt(argv, chat.with_attachments(prompt, ["/u/s1/a.pdf"]))
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")

    def test_chat_codex_turn(self):
        room = chat.ChatSession("sid", "/repo", "codex")
        proc = mock.Mock(stdout=io.StringIO(""), returncode=0)
        for prompt in HOSTILE_PROMPTS:
            with self.subTest(prompt=prompt), \
                    mock.patch.object(chat, "codex_binary", lambda: "codex"), \
                    mock.patch.object(chat.subprocess, "Popen", return_value=proc) as popen:
                room._run_turn(prompt, "auto", "", [])
                argv = popen.call_args.args[0]
                self.assertEqual(argv[-3:], ["--", "sid", prompt])

    def test_background_claude(self):
        for prompt in HOSTILE_PROMPTS:
            with self.subTest(prompt=prompt), \
                    mock.patch.object(models, "listed", return_value=True), \
                    mock.patch.object(web.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="backgrounded · abcdef12", stderr="")
                ok, _, _ = web.spawn_agent("/tmp/repo", prompt, "claude-opus-5", self.ALLOWED)
                self.assertTrue(ok)
                argv = run.call_args.args[0]
                self.assertEqual(argv[:2], ["claude", "--bg"])
                self.assertEndsWithPrompt(argv, prompt)

    def test_background_codex(self):
        for prompt in HOSTILE_PROMPTS:
            with self.subTest(prompt=prompt), \
                    mock.patch.object(web.chat, "codex_binary", lambda: "codex"), \
                    mock.patch.object(web.subprocess, "Popen") as popen:
                ok, _, _ = web.spawn_agent("/tmp/repo", prompt, None, self.ALLOWED, engine="codex")
                self.assertTrue(ok)
                self.assertEndsWithPrompt(popen.call_args.args[0], prompt)

    def test_interactive_claude_and_codex(self):
        for engine in ("claude", "codex"):
            for prompt in HOSTILE_PROMPTS:
                captured = {}

                def fake_tab(command, title):
                    captured["command"] = command
                    return True, "ok"

                with self.subTest(engine=engine, prompt=prompt), \
                        mock.patch.object(web, "_open_terminal_tab", fake_tab), \
                        mock.patch.object(web.sys, "platform", "darwin"), \
                        mock.patch.object(web.chat, "codex_binary", lambda: "codex"):
                    ok, _, _ = web.spawn_agent("/tmp/repo", prompt, None, self.ALLOWED,
                                               engine=engine, interactive=True)
                    self.assertTrue(ok)
                    # What the shell will actually hand the CLI.
                    launched = shlex.split(captured["command"].split(" && ")[-1])
                    self.assertEqual(launched[0], engine)
                    self.assertEndsWithPrompt(launched, prompt)

    def test_the_system_prompt_preamble_is_behind_the_separator_too(self):
        with mock.patch.object(models, "listed", return_value=True), \
                mock.patch.object(web.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="backgrounded · abcdef12", stderr="")
            web.spawn_agent("/tmp/repo", "--version", None, self.ALLOWED, system_prompt="be careful")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2], "--")
        self.assertTrue(argv[-1].startswith("<system instructions>"))
        self.assertTrue(argv[-1].endswith("--version"))


class BodyLengthTests(unittest.TestCase):
    def test_well_formed(self):
        self.assertEqual(web.body_length(None), 0)
        self.assertEqual(web.body_length([]), 0)
        self.assertEqual(web.body_length(["0"]), 0)
        self.assertEqual(web.body_length(["12"]), 12)
        self.assertEqual(web.body_length([" 12 "]), 12)
        self.assertEqual(web.body_length(["7", "7"]), 7)

    def test_malformed_is_refused(self):
        for values in (["-1"], ["-0"], ["+5"], ["abc"], [""], ["1_000"], ["1.5"], ["0x10"],
                       ["١٢"], ["12abc"], ["1e3"], ["5", "6"], ["9" * 13]):
            with self.subTest(values=values):
                self.assertIsNone(web.body_length(values))


class ContentLengthOverHttpTests(unittest.TestCase):
    """The real handler, spoken to over a raw socket so the header can lie."""

    def setUp(self):
        fleet = SimpleNamespace(raw=lambda: [],
                                snapshot=lambda: {"sessions": [], "polledAt": 0.0, "error": None})
        bound = type("BoundHandler", (web.Handler,),
                     {"fleet": fleet, "token": "tok", "chat": None,
                      "log_message": lambda *a, **k: None})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _post(self, headers: list[str], body: bytes = b"") -> tuple[int, bytes]:
        head = ["POST /api/no-such-route HTTP/1.1", "Host: 127.0.0.1",
                "X-Agentgrid-Token: tok", "Content-Type: application/json"] + headers
        raw = ("\r\n".join(head) + "\r\n\r\n").encode() + body
        with socket.create_connection(self.httpd.server_address, timeout=5) as sock:
            sock.sendall(raw)
            # A hang here (the old rfile.read(-1)) fails as a socket timeout.
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            # The server must close after refusing framing it cannot trust.
            sock.settimeout(5)
            rest = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
            except socket.timeout:
                rest = b"<still open>"
        status = int(data.split(b" ", 2)[1])
        return status, rest

    def test_negative_length_is_refused_without_hanging(self):
        status, rest = self._post(["Content-Length: -1"], b"{}")
        self.assertEqual(status, 400)
        self.assertNotIn(b"<still open>", rest)

    def test_non_numeric_length_is_refused(self):
        for value in ("abc", "1_0", "+2", "0x2"):
            with self.subTest(value=value):
                status, rest = self._post([f"Content-Length: {value}"], b"{}")
                self.assertEqual(status, 400)
                self.assertNotIn(b"<still open>", rest)

    def test_conflicting_duplicate_lengths_are_refused(self):
        status, rest = self._post(["Content-Length: 2", "Content-Length: 40"], b"{}")
        self.assertEqual(status, 400)
        self.assertNotIn(b"<still open>", rest)

    def test_oversized_length_is_refused_without_reading_the_body(self):
        status, rest = self._post([f"Content-Length: {web.MAX_BODY_BYTES + 1}"], b"{}")
        self.assertEqual(status, 413)
        self.assertNotIn(b"<still open>", rest)

    def test_a_valid_body_still_reaches_routing(self):
        # Past the length check and the JSON parse: the unknown route answers.
        status, _ = self._post(["Content-Length: 2", "Connection: close"], b"{}")
        self.assertNotIn(status, (400, 413))

    def test_no_length_still_means_an_empty_body(self):
        status, _ = self._post(["Connection: close"])
        self.assertNotIn(status, (400, 413))

    def test_the_token_is_still_checked_first(self):
        head = ["POST /api/no-such-route HTTP/1.1", "Host: 127.0.0.1", "Content-Length: -1"]
        with socket.create_connection(self.httpd.server_address, timeout=5) as sock:
            sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
            self.assertIn(b" 403 ", sock.recv(4096))


if __name__ == "__main__":
    unittest.main()
