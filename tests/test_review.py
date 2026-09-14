"""The web-review overlay: prompt compilation, release routing, and the
routes/CORS that let an overlay on another origin reach the server.

These drive the real HTTP handler (route dispatch, token gate and body parse
included), the same way the dismiss/file route tests do.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import web


class RecordingChat:
    """Stands in for ChatManager, capturing what release() would dispatch."""

    def __init__(self):
        self.sent = []

    def send(self, session_id, cwd, message, posture, model, attachments, engine="claude"):
        self.sent.append({
            "session_id": session_id, "cwd": cwd, "message": message,
            "posture": posture, "model": model, "attachments": attachments,
            "engine": engine,
        })

    def state(self, _session_id):
        return {}


def _serve(sessions=None, chat=None):
    fleet = SimpleNamespace(
        raw=lambda: list(sessions or []),
        snapshot=lambda: {"sessions": [], "polledAt": 0.0, "error": None},
    )
    bound = type("BoundHandler", (web.Handler,),
                 {"fleet": fleet, "token": "tok", "chat": chat,
                  "log_message": lambda *a, **k: None})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _request(port, path, *, method="GET", body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers=headers or {}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


class CompilePromptTests(unittest.TestCase):
    def test_each_kind_names_its_location(self):
        prompt = web._compile_review_prompt(
            "http://localhost:3000/x", "My App",
            [
                {"kind": "text", "quote": "Sign in", "note": "make it bigger"},
                {"kind": "image", "label": "hero.png", "note": "swap render"},
                {"kind": "point", "selector": "div > button", "note": "align right"},
            ],
        )
        self.assertIn("My App", prompt)
        self.assertIn("http://localhost:3000/x", prompt)
        self.assertIn('selected text: "Sign in"', prompt)
        self.assertIn("make it bigger", prompt)
        self.assertIn("the image (hero.png)", prompt)
        self.assertIn("the element `div > button`", prompt)
        # numbered, one item per comment
        self.assertIn("1. On", prompt)
        self.assertIn("2. On", prompt)
        self.assertIn("3. On", prompt)

    def test_long_note_and_quote_are_capped(self):
        prompt = web._compile_review_prompt(
            "u", "t", [{"kind": "text", "quote": "q" * 5000, "note": "z" * 5000}])
        self.assertIn("q" * 600, prompt)
        self.assertNotIn("q" * 601, prompt)
        self.assertIn("z" * 2000, prompt)
        self.assertNotIn("z" * 2001, prompt)


class ReleaseRouteTests(unittest.TestCase):
    def _post_release(self, port, payload, origin="http://localhost:3000"):
        headers = {"Content-Type": "text/plain"}
        if origin:
            headers["Origin"] = origin
        status, hdrs, raw = _request(
            port, "/api/review/release?t=tok", method="POST",
            body=payload, headers=headers)
        return status, hdrs, json.loads(raw or b"{}")

    def test_release_to_running_session_dispatches_to_chat(self):
        chat = RecordingChat()
        session = SimpleNamespace(session_id="s1", cwd="/tmp/proj",
                                  engine="claude", status="idle")
        port = _serve([session], chat).server_address[1]
        status, _hdrs, body = self._post_release(port, {
            "url": "http://localhost:3000/p", "title": "App", "target": "s1",
            "comments": [{"kind": "text", "quote": "Save", "note": "rename to Submit"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["dispatched"], "session")
        self.assertEqual(body["sessionId"], "s1")
        self.assertEqual(len(chat.sent), 1)
        self.assertEqual(chat.sent[0]["session_id"], "s1")
        self.assertEqual(chat.sent[0]["cwd"], "/tmp/proj")
        self.assertIn("rename to Submit", chat.sent[0]["message"])

    def test_no_target_parks_a_pending_review(self):
        chat = RecordingChat()
        port = _serve([], chat).server_address[1]
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(web, "REVIEWS_DIR", Path(base) / "reviews"):
                status, _hdrs, body = self._post_release(port, {
                    "url": "http://localhost:3000/p", "title": "App", "target": None,
                    "comments": [{"kind": "point", "note": "fix spacing"}],
                })
                self.assertEqual(status, 200)
                self.assertEqual(body["dispatched"], "pending")
                files = list((Path(base) / "reviews").glob("*.json"))
                self.assertEqual(len(files), 1)
                saved = json.loads(files[0].read_text("utf-8"))
                self.assertIn("fix spacing", saved["prompt"])
                self.assertEqual(saved["comments"][0]["note"], "fix spacing")
        # nothing was dispatched to a session
        self.assertEqual(chat.sent, [])

    def test_unknown_target_is_a_404(self):
        port = _serve([], RecordingChat()).server_address[1]
        status, _hdrs, body = self._post_release(
            port, {"target": "ghost", "comments": [{"kind": "point", "note": "x"}]})
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_empty_batch_is_a_400(self):
        port = _serve([], RecordingChat()).server_address[1]
        status, _hdrs, body = self._post_release(port, {"comments": []})
        self.assertEqual(status, 400)


class OverlayRoutesTests(unittest.TestCase):
    def test_overlay_js_is_served_as_javascript(self):
        port = _serve().server_address[1]
        status, hdrs, raw = _request(port, "/overlay.js?t=tok")
        self.assertEqual(status, 200)
        self.assertIn("javascript", hdrs.get("Content-Type", ""))
        self.assertIn(b"__agReviewMounted", raw)

    def test_overlay_js_needs_the_token(self):
        port = _serve().server_address[1]
        status, _hdrs, _raw = _request(port, "/overlay.js")
        self.assertEqual(status, 403)

    def test_review_install_page_has_bookmarklet_with_token(self):
        port = _serve().server_address[1]
        status, hdrs, raw = _request(port, "/review?t=tok")
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs.get("Content-Type", ""))
        page = raw.decode("utf-8")
        self.assertIn("overlay.js?ag=", page)
        # the live token is baked into the bookmarklet
        self.assertIn("tok", page)


class CorsTests(unittest.TestCase):
    def test_sessions_reflects_the_overlay_origin(self):
        port = _serve([], RecordingChat()).server_address[1]
        status, hdrs, _raw = _request(
            port, "/api/sessions?t=tok",
            headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 200)
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "http://localhost:3000")
        self.assertEqual(hdrs.get("Vary"), "Origin")

    def test_non_overlay_route_does_not_get_cors(self):
        port = _serve([], RecordingChat()).server_address[1]
        # /api/models is same-origin only; no ACAO even with an Origin header
        status, hdrs, _raw = _request(
            port, "/api/models?t=tok",
            headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 200)
        self.assertIsNone(hdrs.get("Access-Control-Allow-Origin"))

    def test_preflight_options_is_allowed_for_release(self):
        port = _serve().server_address[1]
        status, hdrs, _raw = _request(
            port, "/api/review/release?t=tok", method="OPTIONS",
            headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 204)
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "http://localhost:3000")
        self.assertIn("POST", hdrs.get("Access-Control-Allow-Methods", ""))


if __name__ == "__main__":
    unittest.main()
