"""The connection file a local companion (Chief of Staff) reads to find this run.

It carries the same token the printed URL does, so the checks are that only the
owner can read it, and that a run only ever removes its own file.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agentgrid import web


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


if __name__ == "__main__":
    unittest.main()
