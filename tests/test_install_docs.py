"""`ag --version`, and the install docs an outside user starts from (AG2-4).

The docs half guards against drift: a link to a file or heading that moved, a
flag the docs mention that the CLI no longer has, the placeholder clone URL
coming back, or a new file under ~/.agentgrid that the backup section's list
does not name.
"""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agentgrid
from agentgrid import __main__ as cli

REPO = Path(__file__).resolve().parent.parent
CLONE_URL = "https://github.com/dimichoueiry/agentgrid.git"
DOCS = ["README.md", "docs/OPERATING.md", "docs/USAGE.md", "extension/README.md"]


def _run_main(argv: list[str]) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            cli.main(argv)
        except SystemExit as exc:  # --help exits 0
            if exc.code not in (0, None):
                raise
    return out.getvalue()


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to -."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    return {_slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+)$", text, re.M)}


class VersionTest(unittest.TestCase):
    def test_version_is_a_release_number(self):
        self.assertRegex(agentgrid.__version__, r"^\d+\.\d+\.\d+$")

    def test_flag_prints_version_and_does_not_launch(self):
        with mock.patch("agentgrid.ui.main") as grid:
            out = _run_main(["--version"])
        self.assertTrue(out.startswith(f"ag {agentgrid.__version__}"), out)
        grid.assert_not_called()
        self.assertEqual(_run_main(["-V"]), out)

    def test_without_git_it_is_the_release_alone(self):
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(cli.version_string(), f"ag {agentgrid.__version__}")

    def test_a_checkout_names_its_commit(self):
        done = subprocess.CompletedProcess([], 0, stdout=f"{REPO}\nabc1234\n")
        with mock.patch("subprocess.run", return_value=done):
            self.assertEqual(cli.version_string(), f"ag {agentgrid.__version__} (abc1234)")

    def test_a_copy_inside_another_repo_does_not_borrow_its_commit(self):
        done = subprocess.CompletedProcess([], 0, stdout="/somewhere/else\nabc1234\n")
        with mock.patch("subprocess.run", return_value=done):
            self.assertEqual(cli.version_string(), f"ag {agentgrid.__version__}")

    def test_launcher_end_to_end(self):
        result = subprocess.run([str(REPO / "bin" / "ag"), "--version"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(f"ag {agentgrid.__version__}"))

    def test_launcher_runs_its_own_clone_not_the_cwd(self):
        # `python3 -m` would import ./agentgrid first; running `ag` from inside
        # another checkout must still run the clone the launcher lives in.
        with tempfile.TemporaryDirectory() as other:
            fake = Path(other) / "agentgrid"
            fake.mkdir()
            (fake / "__init__.py").write_text("")
            (fake / "__main__.py").write_text("print('WRONG CHECKOUT')\n")
            result = subprocess.run([str(REPO / "bin" / "ag"), "--version"], cwd=other,
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(f"ag {agentgrid.__version__}"), result.stdout)

    def test_ticket_verbs_are_untouched(self):
        # --version belongs to the grid's parser; `ag ticket` is dispatched
        # before it is built and must not see the flag at all.
        with mock.patch("agentgrid.ticket_cli.main", return_value=0) as tickets:
            with self.assertRaises(SystemExit):
                cli.main(["ticket", "show", "AG-1"])
        tickets.assert_called_once_with(["show", "AG-1"])


class InstallDocsTest(unittest.TestCase):
    def test_quick_start_is_near_the_top_with_the_real_clone_url(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        head = "\n".join(readme.splitlines()[:40])
        self.assertIn("## Quick start", head)
        self.assertIn(f"git clone {CLONE_URL}", head)
        self.assertIn("docs/OPERATING.md", head)
        self.assertIn("extension/README.md", head)

    def test_no_placeholder_clone_url(self):
        for name in DOCS:
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertNotIn("<this repo>", text, name)
            for url in re.findall(r"git clone (\S+)", text):
                self.assertEqual(url, CLONE_URL, name)

    def test_operating_guide_covers_the_lifecycle(self):
        text = (REPO / "docs" / "OPERATING.md").read_text(encoding="utf-8")
        for heading in ("Requirements", "Supported platforms", "Install",
                        "Launch, stop, restart", "Keep it running", "Upgrade",
                        "Persistence and backup", "Uninstall"):
            self.assertIn(f"## {heading}\n", text)

    def test_relative_links_and_anchors_resolve(self):
        for name in DOCS:
            source = REPO / name
            text = re.sub(r"```.*?```", "", source.read_text(encoding="utf-8"), flags=re.S)
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"[a-z]+:", target):
                    continue  # external
                path, _, anchor = target.partition("#")
                resolved = (source.parent / path).resolve() if path else source
                self.assertTrue(resolved.exists(), f"{name}: {target}")
                if anchor and resolved.suffix == ".md":
                    self.assertIn(anchor, _anchors(resolved), f"{name}: {target}")

    def test_every_documented_flag_exists(self):
        known = set(re.findall(r"--[a-z][a-z-]*", _run_main(["--help"])))
        for name in ("README.md", "docs/OPERATING.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            for line in re.findall(r"(?:^|[`'\s])ag (--[^`'\n#]*)", text, re.M):
                for flag in re.findall(r"--[a-z][a-z-]*", line):
                    self.assertIn(flag, known, f"{name}: ag {line.strip()}")

    def test_state_on_disk_lists_everything_under_agentgrid_home(self):
        # The backup advice points at this list; a new store nobody added to it
        # is a store nobody knows to back up.
        written = set()
        for source in (REPO / "agentgrid").glob("*.py"):
            written.update(re.findall(r"""['"]\.agentgrid['"]\s*/\s*['"]([^'"]+)['"]""",
                                      source.read_text(encoding="utf-8")))
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("## State on disk"):]
        tree = section[:section.index("```", section.index("```") + 3)]
        self.assertGreater(len(written), 10)
        for entry in sorted(written):
            self.assertRegex(tree, rf"[─ ]{re.escape(entry)}/?\s", entry)


if __name__ == "__main__":
    unittest.main()
