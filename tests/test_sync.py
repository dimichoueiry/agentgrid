"""Notes sync, exercised fully offline against a bare git repo as the remote.

Every test stands up a real bare repository in a temp dir and points one or
more "machines" (each its own notes folder, patched onto notes.NOTES_DIR) at
it. That runs the actual git plumbing sync uses -- init, commit, fetch, rebase,
push -- with no network and no GitHub, so the round trip and, crucially, the
never-lose-a-note conflict behaviour are asserted for real rather than mocked.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentgrid import notes, sync


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


@unittest.skipUnless(_git_available(), "git is required for sync tests")
class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        remote = self.base / "remote.git"
        done = subprocess.run(["git", "init", "--bare", str(remote)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.remote = str(remote)

    # A machine is just a notes folder; patching notes' two path constants
    # relocates the whole sync onto it, because sync derives its paths from them.
    def _as(self, notes_dir: Path):
        notes_dir.mkdir(parents=True, exist_ok=True)
        return mock.patch.multiple(
            notes, NOTES_DIR=notes_dir, META_PATH=notes_dir.parent / "note-meta.json"
        )

    @staticmethod
    def _write_note(notes_dir: Path, day: str, text: str, page: str = "Notes") -> Path:
        folder = notes_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{page}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def _sync(self, notes_dir: Path, branch: str = "main"):
        with self._as(notes_dir):
            return sync.sync_notes(self.remote, branch)

    # -- the happy path ------------------------------------------------------

    def test_first_machine_pushes_and_second_adopts(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "hello from A\n")
        ok, msg = self._sync(a)
        self.assertTrue(ok, msg)

        # b starts empty: it should adopt the remote outright, not collide.
        b = self.base / "b" / "notes"
        ok, msg = self._sync(b)
        self.assertTrue(ok, msg)
        landed = b / "2026-08-06" / "Notes.md"
        self.assertTrue(landed.is_file())
        self.assertEqual(landed.read_text(encoding="utf-8"), "hello from A\n")

    def test_changes_flow_both_ways(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "from A\n")
        self.assertTrue(self._sync(a)[0])
        b = self.base / "b" / "notes"
        self.assertTrue(self._sync(b)[0])

        # b adds a note and pushes; a pulls it on its next sync.
        self._write_note(b, "2026-08-07", "from B\n")
        self.assertTrue(self._sync(b)[0])
        ok, msg = self._sync(a)
        self.assertTrue(ok, msg)
        self.assertTrue((a / "2026-08-07" / "Notes.md").is_file())

    def test_page_metadata_rides_along(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "x\n")
        (a.parent / "note-meta.json").write_text(json.dumps({"groups": {"Work": ["Notes"]}}),
                                                 encoding="utf-8")
        self.assertTrue(self._sync(a)[0])

        b = self.base / "b" / "notes"
        self.assertTrue(self._sync(b)[0])
        restored = b.parent / "note-meta.json"
        self.assertTrue(restored.is_file())
        self.assertEqual(json.loads(restored.read_text())["groups"], {"Work": ["Notes"]})

    def test_syncing_with_no_changes_is_still_ok(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "x\n")
        self.assertTrue(self._sync(a)[0])
        ok, msg = self._sync(a)  # nothing changed since
        self.assertTrue(ok, msg)

    # -- the safety net ------------------------------------------------------

    def test_a_true_conflict_is_reported_and_local_notes_are_kept(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "base\n")
        self.assertTrue(self._sync(a)[0])
        b = self.base / "b" / "notes"
        self.assertTrue(self._sync(b)[0])

        # a and b edit the SAME line differently, and a pushes first.
        self._write_note(a, "2026-08-06", "A wins\n")
        self.assertTrue(self._sync(a)[0])
        self._write_note(b, "2026-08-06", "B wins\n")
        ok, msg = self._sync(b)

        self.assertFalse(ok)
        self.assertIn("conflict", msg.lower())
        # The whole point: b's note is untouched, not clobbered by the failed pull.
        self.assertEqual((b / "2026-08-06" / "Notes.md").read_text(encoding="utf-8"), "B wins\n")

    def test_a_bad_remote_is_refused_before_touching_git(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "x\n")
        with self._as(a):
            ok, msg = sync.sync_notes("not a url", "main")
        self.assertFalse(ok)
        self.assertFalse((a / ".git").exists())  # nothing was initialised

    def test_a_bad_branch_name_is_refused(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "x\n")
        with self._as(a):
            ok, _msg = sync.sync_notes(self.remote, "--force")
        self.assertFalse(ok)

    def test_config_persists_the_remote_even_when_the_push_fails(self):
        a = self.base / "a" / "notes"
        self._write_note(a, "2026-08-06", "x\n")
        unreachable = str(self.base / "does-not-exist.git")
        with self._as(a):
            ok, _msg = sync.sync_notes(unreachable, "main")
            self.assertFalse(ok)
            # The URL survives, so the sheet comes back filled in, not blank.
            self.assertEqual(sync.load_config().get("remote"), unreachable)
