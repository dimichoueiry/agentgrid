"""A project-less agent spawns from the Desktop, with the cwd resolved on the
server rather than trusted from the client.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import web


def _serve():
    fleet = SimpleNamespace(
        raw=lambda: [],
        snapshot=lambda: {"sessions": [], "polledAt": 0.0, "error": None},
        name_when_seen=lambda *a, **k: None,
    )
    bound = type("BoundHandler", (web.Handler,),
                 {"fleet": fleet, "token": "tok", "chat": None,
                  "log_message": lambda *a, **k: None})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _post(port, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/spawn?t=tok",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


class FreeformRootTests(unittest.TestCase):
    def test_prefers_desktop_else_home(self):
        root = web.freeform_root()
        self.assertTrue(root.is_dir())
        self.assertIn(root, (Path.home() / "Desktop", Path.home()))


class NoProjectSpawnTests(unittest.TestCase):
    def test_no_project_spawns_from_desktop_and_passes_the_boundary(self):
        captured = {}

        def recorder(cwd, prompt, model, allowed, engine="claude",
                     system_prompt="", interactive=False):
            captured["cwd"] = cwd
            captured["allowed"] = [p["path"] for p in allowed]
            return True, "Started.", "job-1"

        with mock.patch.object(web, "spawn_agent", recorder):
            port = _serve().server_address[1]
            status, body = _post(port, {"noProject": True, "prompt": "look around"})

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        # cwd is the server-resolved Desktop, never taken from the client
        self.assertEqual(captured["cwd"], str(web.freeform_root()))
        # and that same root is what the spawn boundary was allowed to accept
        self.assertIn(str(web.freeform_root()), captured["allowed"])

    def test_client_cwd_is_ignored_when_no_project_is_set(self):
        captured = {}

        def recorder(cwd, *a, **k):
            captured["cwd"] = cwd
            return True, "Started.", None

        with mock.patch.object(web, "spawn_agent", recorder):
            port = _serve().server_address[1]
            _post(port, {"noProject": True, "cwd": "/etc", "prompt": "x"})

        self.assertEqual(captured["cwd"], str(web.freeform_root()))
        self.assertNotEqual(captured["cwd"], "/etc")


if __name__ == "__main__":
    unittest.main()
