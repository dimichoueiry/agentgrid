"""Marked-up screenshots from the review overlay: saved and attached to the
agent's turn on dispatch, and referenced by path when parked.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import web

# A real 1x1 PNG, so magic-byte validation passes.
PNG_1x1 = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class RecordingChat:
    def __init__(self):
        self.sent = []

    def send(self, session_id, cwd, message, posture, model, attachments, engine="claude"):
        self.sent.append({"message": message, "attachments": list(attachments or [])})

    def state(self, _):
        return {}


def _serve(session, chat):
    fleet = SimpleNamespace(raw=lambda: [session] if session else [])
    bound = type("BoundHandler", (web.Handler,),
                 {"fleet": fleet, "token": "tok", "chat": chat,
                  "log_message": lambda *a, **k: None})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _release(port, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/review/release?t=tok",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "text/plain"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=5)
    return resp.status, json.loads(resp.read())


class ScreenshotAttachTests(unittest.TestCase):
    def test_drawing_is_saved_and_attached_to_the_turn(self):
        chat = RecordingChat()
        session = SimpleNamespace(session_id="s1", cwd="/tmp/proj", engine="claude")
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(web, "UPLOADS_DIR", Path(base)):
                port = _serve(session, chat).server_address[1]
                status, body = _release(port, {
                    "target": "s1", "url": "http://localhost:3000/x", "title": "App",
                    "comments": [{"kind": "point", "note": "circle -> change to blue",
                                  "image": PNG_1x1}],
                })
                self.assertEqual(status, 200)
                self.assertEqual(body["attached"], 1)
                self.assertEqual(len(chat.sent), 1)
                attached = chat.sent[0]["attachments"]
                self.assertEqual(len(attached), 1)
                # the saved file exists, under this session's uploads dir
                self.assertTrue(Path(attached[0]).is_file())
                self.assertIn("s1", attached[0])
                # and the prompt points the agent at it
                self.assertIn(attached[0], chat.sent[0]["message"])

    def test_invalid_image_does_not_lose_the_comment(self):
        chat = RecordingChat()
        session = SimpleNamespace(session_id="s1", cwd="/tmp/proj", engine="claude")
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(web, "UPLOADS_DIR", Path(base)):
                port = _serve(session, chat).server_address[1]
                status, body = _release(port, {
                    "target": "s1",
                    "comments": [{"kind": "point", "note": "still here",
                                  "image": "data:image/png;base64,not-real"}],
                })
                self.assertEqual(status, 200)
                self.assertEqual(body["attached"], 0)
                self.assertIn("still here", chat.sent[0]["message"])

    def test_parked_review_writes_image_file_and_lean_json(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(web, "REVIEWS_DIR", Path(base) / "reviews"):
                port = _serve(None, RecordingChat()).server_address[1]
                status, body = _release(port, {
                    "target": None,
                    "comments": [{"kind": "point", "note": "fix", "image": PNG_1x1}],
                })
                self.assertEqual(status, 200)
                self.assertEqual(body["dispatched"], "pending")
                saved = json.loads(Path(body["path"]).read_text("utf-8"))
                # base64 is stripped from the stored record ...
                self.assertNotIn("image", saved["comments"][0])
                # ... but the file was written and is referenced by path
                imgpath = saved["comments"][0]["_imgpath"]
                self.assertTrue(Path(imgpath).is_file())
                self.assertIn(imgpath, saved["prompt"])


if __name__ == "__main__":
    unittest.main()
