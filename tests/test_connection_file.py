"""The connection file a local companion (Chief of Staff) reads to find this run.

It carries the same token the printed URL does, so the checks are that only the
owner can read it, that a run only ever removes its own file, and that a run
keeps the file naming itself for as long as it lives: anything else and the
companion says agentgrid is not running while it plainly is (AG-3).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentgrid import web


def _record(port: int, token: str, pid: int) -> dict:
    return {"url": f"http://127.0.0.1:{port}", "port": port, "token": token, "pid": pid}


class _Ping(BaseHTTPRequestHandler):
    """Answers /api/ping like a live run does, for one token only."""
    token = "live-run"

    def do_GET(self):  # noqa: N802
        # Like a run: the token is checked before the route, and an older run
        # that predates /api/ping answers a good token with 404.
        good = self.headers.get("X-Agentgrid-Token") == self.token
        ok = good and self.path == "/api/ping"
        self.send_response(200 if ok else 404 if good else 403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}' if ok else b'{"error": "no"}')

    def log_message(self, *_):
        pass


class ConnectionFileTests(unittest.TestCase):
    def test_written_owner_only_with_port_token_and_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "web.json"
            web.write_connection_file(8787, "tok-1", path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["url"], "http://127.0.0.1:8787")
            self.assertEqual((data["port"], data["token"], data["pid"]), (8787, "tok-1", os.getpid()))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_run_removes_only_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.json"
            web.write_connection_file(8787, "old-run", path)
            web.write_connection_file(8788, "new-run", path)
            web.remove_connection_file("old-run", path)
            self.assertTrue(path.exists())
            web.remove_connection_file("new-run", path)
            self.assertFalse(path.exists())
            web.remove_connection_file("new-run", path)  # already gone: no error


class ClaimTests(unittest.TestCase):
    """claim_connection_file: take the file unless a run that still answers holds it."""

    def test_a_missing_or_garbled_file_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.json"
            self.assertTrue(web.claim_connection_file(8787, "me", path, answers=lambda r: True))
            self.assertEqual(web.read_connection_file(path)["token"], "me")
            path.write_text("not json", encoding="utf-8")
            self.assertTrue(web.claim_connection_file(8787, "me", path, answers=lambda r: True))
            self.assertEqual(web.read_connection_file(path)["token"], "me")

    def test_a_file_held_by_a_live_run_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.json"
            path.write_text(json.dumps(_record(8787, "live-run", 4242)), encoding="utf-8")
            asked = []
            taken = web.claim_connection_file(8788, "verification-run", path, answers=lambda r: asked.append(r) or True)
            self.assertFalse(taken)
            self.assertEqual(web.read_connection_file(path)["token"], "live-run")
            self.assertEqual(asked[0]["pid"], 4242)

    def test_a_file_left_by_a_dead_run_is_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.json"
            path.write_text(json.dumps(_record(8788, "killed-run", 4242)), encoding="utf-8")
            self.assertTrue(web.claim_connection_file(8787, "me", path, answers=lambda r: False))
            self.assertEqual(web.read_connection_file(path)["token"], "me")

    def test_our_own_file_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.json"
            web.write_connection_file(8787, "me", path)
            before = path.stat().st_mtime_ns
            probed = []
            self.assertTrue(web.claim_connection_file(8787, "me", path, answers=lambda r: probed.append(r)))
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(probed, [])  # never probes itself

    def test_the_keeper_puts_the_file_back_when_it_goes_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.json"
            web.write_connection_file(8787, "me", path)
            path.unlink()
            stop = threading.Event()
            thread = threading.Thread(target=web.keep_connection_file, args=(8787, "me", stop, path, 0.01))
            thread.start()
            try:
                for _ in range(200):
                    if path.exists():
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(web.read_connection_file(path)["token"], "me")
            finally:
                stop.set()
                thread.join(2)


class AnswersTests(unittest.TestCase):
    """connection_answers: only a 200 from /api/ping with the file's token counts."""

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Ping)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_a_run_that_answers_with_its_token_is_alive(self):
        self.assertTrue(web.connection_answers(_record(self.port, "live-run", os.getpid())))

    def test_a_different_token_on_that_port_is_another_run(self):
        self.assertFalse(web.connection_answers(_record(self.port, "old-run", os.getpid())))

    def test_a_run_older_than_the_ping_route_still_counts_as_alive(self):
        record = _record(self.port, "live-run", os.getpid())
        record["url"] = f"http://127.0.0.1:{self.port}/older"  # its ping lands on a route it lacks: 404
        self.assertTrue(web.connection_answers(record))

    def test_a_dead_pid_or_a_closed_port_is_gone(self):
        self.assertFalse(web.connection_answers(_record(self.port, "live-run", 2 ** 22 + 7)))
        spare = ThreadingHTTPServer(("127.0.0.1", 0), _Ping)
        closed = spare.server_address[1]
        spare.server_close()
        self.assertFalse(web.connection_answers(_record(closed, "live-run", os.getpid())))


if __name__ == "__main__":
    unittest.main()
