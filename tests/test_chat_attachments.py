"""Chat attachments beyond pasted images: what the upload route accepts (by
the file's bytes), how each refusal reads, and how the saved files reach the
agent for both engines.
"""
from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import chat, web
from support import isolate_chat_queue

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                       "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"


def office_zip(content_types: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as package:
        if content_types:
            package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


class AttachmentTypeTests(unittest.TestCase):
    def test_images_and_pdfs_are_known_by_their_bytes(self):
        self.assertEqual(web.attachment_type(PNG, "shot.jpg"), ("image", "png"))
        self.assertEqual(web.attachment_type(PDF, "notes"), ("pdf", "pdf"))

    def test_text_keeps_its_own_extension(self):
        self.assertEqual(web.attachment_type(b"export const x = 1;\n", "app.TSX"), ("text", "tsx"))
        self.assertEqual(web.attachment_type("naïve,1\n".encode(), "data.csv"), ("text", "csv"))
        self.assertEqual(web.attachment_type(b"\xef\xbb\xbfbom text", "a.md"), ("text", "md"))

    def test_text_without_a_usable_extension_becomes_txt(self):
        self.assertEqual(web.attachment_type(b"all: build\n", "Makefile"), ("text", "txt"))
        self.assertEqual(web.attachment_type(b"x", "../../etc/pa.ss/wd"), ("text", "txt"))
        # a text file posing as a binary format is not saved under that name
        self.assertEqual(web.attachment_type(b"not a pdf", "fake.pdf"), ("text", "txt"))
        self.assertEqual(web.attachment_type(b"not a doc", "fake.docx"), ("text", "txt"))

    def test_office_documents_must_really_be_office_packages(self):
        self.assertEqual(web.attachment_type(office_zip(), "Plan.DOCX"), ("document", "docx"))
        self.assertEqual(web.attachment_type(office_zip(), "sheet.xlsx"), ("document", "xlsx"))
        self.assertIsNone(web.attachment_type(office_zip(content_types=False), "plan.docx"))
        self.assertIsNone(web.attachment_type(office_zip(), "archive.zip"))
        self.assertIsNone(web.attachment_type(b"PK\x03\x04garbage", "plan.docx"))

    def test_binaries_are_refused(self):
        self.assertIsNone(web.attachment_type(b"\xcf\xfa\xed\xfe\x07\x00\x00\x01", "tool"))
        self.assertIsNone(web.attachment_type(b"MZ\x90\x00\x03\x00", "setup.exe"))
        self.assertIsNone(web.attachment_type(b"\xff\xfe\xfd not utf-8", "a.txt"))


def _serve(session):
    fleet = SimpleNamespace(raw=lambda: [session])
    bound = type("BoundHandler", (web.Handler,),
                 {"fleet": fleet, "token": "tok", "chat": mock.Mock(),
                  "log_message": lambda *a, **k: None})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class UploadRouteTests(unittest.TestCase):
    def setUp(self):
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.root = Path(base.name)
        patcher = mock.patch.object(web, "UPLOADS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        session = SimpleNamespace(session_id="s1", cwd="/tmp/proj", engine="claude")
        self.httpd = _serve(session)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def upload(self, raw: bytes, name: str):
        payload = {"sessionId": "s1", "name": name,
                   "data": "data:application/octet-stream;base64," + base64.b64encode(raw).decode()}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.httpd.server_address[1]}/api/chat/upload?t=tok",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_pdf_is_saved_under_the_session_and_reported_as_one(self):
        status, body = self.upload(PDF, "Q3 report.pdf")
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "pdf")
        saved = Path(body["path"])
        self.assertEqual(saved.parent, self.root / "s1")
        self.assertTrue(saved.name.endswith("-Q3-report.pdf"))
        self.assertEqual(saved.read_bytes(), PDF)

    def test_code_and_office_files_are_accepted(self):
        status, body = self.upload(b"print('hi')\n", "main.py")
        self.assertEqual((status, body["kind"]), (200, "text"))
        self.assertTrue(body["path"].endswith("-main.py"))
        status, body = self.upload(office_zip(), "deck.pptx")
        self.assertEqual((status, body["kind"]), (200, "document"))

    def test_an_unsupported_file_is_refused_by_name_with_what_would_work(self):
        status, body = self.upload(b"MZ\x90\x00\x03\x00", "setup.exe")
        self.assertEqual(status, 415)
        self.assertIn("setup.exe can't be attached", body["error"])
        self.assertIn("PDF", body["error"])
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_empty_and_oversize_files_say_so(self):
        status, body = self.upload(b"", "blank.txt")
        self.assertEqual(status, 400)
        self.assertIn("blank.txt", body["error"])
        with mock.patch.object(web, "MAX_UPLOAD_BYTES", 8):
            status, body = self.upload(b"123456789", "big.log")
        self.assertEqual(status, 413)
        self.assertIn("big.log is larger than", body["error"])

    def test_the_saved_file_passes_the_send_path_check(self):
        _, body = self.upload(b"a,b\n1,2\n", "rows.csv")
        handler = object.__new__(web.Handler)
        session = SimpleNamespace(session_id="s1")
        kept = handler._valid_attachments([body["path"], "/etc/hosts"], session)
        self.assertEqual(kept, [str(Path(body["path"]).resolve())])


class PromptTests(unittest.TestCase):
    def setUp(self):
        isolate_chat_queue(self)

    def test_images_keep_their_wording(self):
        self.assertEqual(chat.with_attachments("see", ["/u/a.png"]),
                         "see\n\nAttached image:\n- /u/a.png")

    def test_any_other_file_is_named_as_a_file(self):
        self.assertEqual(chat.with_attachments("see", ["/u/a.png", "/u/b.pdf"]),
                         "see\n\nAttached files:\n- /u/a.png\n- /u/b.pdf")
        self.assertEqual(chat.with_attachments("", ["/u/log.txt"]),
                         "Please look at the attached file(s):\n- /u/log.txt")

    def test_claude_is_granted_the_uploads_folder_so_read_only_can_open_it(self):
        room = chat.ChatSession("sid", "/repo", "claude")
        proc = mock.Mock(stdout=io.StringIO(""), returncode=0)
        with mock.patch.object(chat.subprocess, "Popen", return_value=proc) as popen:
            room._run_turn("look", "read-only", "", ["/u/s1/a.pdf", "/u/s1/b.txt"])
        argv = popen.call_args.args[0]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"], ["/u/s1"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("-p") + 1],
                         "look\n\nAttached files:\n- /u/s1/a.pdf\n- /u/s1/b.txt")
        # a turn without attachments grants nothing extra
        with mock.patch.object(chat.subprocess, "Popen", return_value=proc) as popen:
            room._run_turn("hi", "auto", "", [])
        self.assertNotIn("--add-dir", popen.call_args.args[0])

    def test_codex_gets_images_natively_and_other_files_in_the_prompt(self):
        room = chat.ChatSession("thread", "/repo", "codex")
        proc = mock.Mock(stdout=io.StringIO(""), returncode=0)
        with mock.patch.object(chat, "codex_binary", return_value="codex"), \
                mock.patch.object(chat.subprocess, "Popen", return_value=proc) as popen:
            room._run_turn("review these", "auto", "", ["/u/shot.PNG", "/u/spec.pdf"])
        argv = popen.call_args.args[0]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--image"], ["/u/shot.PNG"])
        self.assertEqual(argv[-1], "review these\n\nAttached file:\n- /u/spec.pdf")

    def test_codex_image_only_turn_still_gets_a_nudge(self):
        room = chat.ChatSession("thread", "/repo", "codex")
        proc = mock.Mock(stdout=io.StringIO(""), returncode=0)
        with mock.patch.object(chat, "codex_binary", return_value="codex"), \
                mock.patch.object(chat.subprocess, "Popen", return_value=proc) as popen:
            room._run_turn("", "auto", "", ["/u/shot.png"])
        self.assertEqual(popen.call_args.args[0][-1], "Please look at the attached image(s).")


if __name__ == "__main__":
    unittest.main()
