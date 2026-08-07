"""Push and pull the notes folder to a git remote the user names.

Notes already live as plain markdown under ``~/.agentgrid/notes/``, so syncing
them is a git problem, not a bespoke one: this module turns that folder into a
small git repository and, on demand, commits what changed, pulls what the
remote has, and pushes. The transport is the user's *own* git -- their SSH keys
or credential helper -- so agentgrid never stores a token or a password, which
keeps this in line with the loopback, no-secrets posture of the rest of the
app.

Every step fails soft and, above all, never loses a note. The order is the
guarantee: local changes are committed *before* anything is pulled, the pull is
a rebase that is *aborted* (never forced through) the moment it conflicts, and
nothing here ever force-pushes or resets hard. A conflict is reported for a
human to resolve, which is the honest ceiling for an automatic sync. A second
machine whose notes folder is empty adopts the remote history outright rather
than colliding with it.

The one file that does not live inside the notes folder -- ``note-meta.json``,
the page ordering and grouping, which sits one level up -- rides along as a
copy inside the repo and is restored after a pull. It is neither a dated folder
nor a ``.md`` file, so the notes reader ignores it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from agentgrid import notes

DEFAULT_BRANCH = "main"

# A remote we are willing to hand to git: an SSH or HTTPS URL, or a local path.
# The leading-dash guard matters even with argv (no shell): `git remote add
# origin -x` would otherwise let a crafted "url" pose as a git option.
_REMOTE_RE = re.compile(r"^(https://|git@|ssh://|git://|/|~|\./|\.\./|[A-Za-z]:\\)")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")


# ---------------------------------------------------------------------------
# Paths. Derived from notes' own constants at call time, so a test that points
# notes.NOTES_DIR at a temp directory relocates the whole sync with it.


def _notes_dir() -> Path:
    return notes.NOTES_DIR


def _meta_path() -> Path:
    return notes.META_PATH


def _meta_in_repo() -> Path:
    return notes.NOTES_DIR / "note-meta.json"


def _sync_path() -> Path:
    return notes.NOTES_DIR.parent / "sync.json"


# ---------------------------------------------------------------------------
# Config: the remote and branch, plus the last outcome. Same atomic-write
# discipline as every other state file.


def load_config() -> dict:
    try:
        data = json.loads(_sync_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(**fields) -> dict:
    config = load_config()
    config.update(fields)
    path = _sync_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)
    return config


# ---------------------------------------------------------------------------
# Git plumbing. Every call runs inside the notes repo and never raises: a
# failure is a return code and stderr for the caller to interpret.


def _git(*args: str, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(_notes_dir()),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _trim(prefix: str, done: subprocess.CompletedProcess) -> str:
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    tail = detail[-1][:160] if detail else ""
    return f"{prefix}: {tail}" if tail else f"{prefix}."


def _auth_hint(done: subprocess.CompletedProcess) -> str:
    text = (done.stderr or "").lower()
    if any(w in text for w in ("permission", "authentication", "could not read", "denied",
                               "publickey", "not found")):
        return (" Check your git access to this repo -- an SSH key, or `gh auth login` "
                "for HTTPS -- and that the repository exists.")
    return ""


def _snapshot_meta() -> None:
    """Copy the sidecar note-meta.json into the repo so it is committed too."""
    meta = _meta_path()
    if meta.is_file():
        try:
            shutil.copyfile(meta, _meta_in_repo())
        except OSError:
            pass


def _restore_meta() -> None:
    """Copy a pulled note-meta.json back to where the notes reader looks."""
    repo_meta = _meta_in_repo()
    if repo_meta.is_file():
        try:
            shutil.copyfile(repo_meta, _meta_path())
        except OSError:
            pass


def _has_local_notes() -> bool:
    notes_dir = _notes_dir()
    return any(notes_dir.glob("*/*.md")) or any(notes_dir.glob("*.md"))


# ---------------------------------------------------------------------------
# The sync itself.


def _ensure_repo(remote: str, branch: str) -> tuple[bool, str] | None:
    """Make the notes folder a git repo aimed at `remote`/`branch`.

    Returns an error tuple to short-circuit, or None to carry on. On a *fresh*
    repo whose remote branch already has notes and whose local folder has none,
    the remote history is adopted outright -- that is what lets a second machine
    start from the shared notes instead of a spurious unrelated-histories clash.
    """
    notes_dir = _notes_dir()
    fresh = not (notes_dir / ".git").is_dir()
    if fresh:
        init = _git("init")
        if init.returncode != 0:
            return False, _trim("git init failed", init)

    if _git("remote", "get-url", "origin").returncode == 0:
        _git("remote", "set-url", "origin", remote)
    else:
        _git("remote", "add", "origin", remote)

    if fresh:
        _git("fetch", "origin", branch)
        remote_has = _git("rev-parse", "--verify", f"origin/{branch}").returncode == 0
        if remote_has and not _has_local_notes():
            # Nothing local to lose, so take the remote history wholesale. No
            # .gitignore is written first: it would be an untracked file the
            # checkout refuses to overwrite with the remote's own copy.
            adopt = _git("checkout", "-B", branch, f"origin/{branch}")
            if adopt.returncode != 0:
                return False, _trim("could not adopt the remote notes", adopt)
            _restore_meta()
            return None

    _git("checkout", "-B", branch)
    # Written only on the non-adopting path, and only if absent, so it never
    # collides with a .gitignore arriving from the remote.
    ignore = notes_dir / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text("*.tmp\n", encoding="utf-8")
        except OSError:
            pass
    return None


def sync_notes(remote: str, branch: str = DEFAULT_BRANCH) -> tuple[bool, str]:
    """Commit local notes, pull the remote, and push. Never raises.

    The remote and branch are persisted *before* the network is touched, so the
    URL you typed survives even when the sync itself fails (bad key, no repo),
    and the button comes back pre-filled rather than blank.
    """
    remote = (remote or "").strip()
    branch = (branch or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
    if not remote or remote.startswith("-") or not _REMOTE_RE.match(remote):
        return False, "That does not look like a git remote -- use an SSH or HTTPS GitHub URL."
    if branch.startswith("-") or not _BRANCH_RE.match(branch):
        return False, "That branch name is not valid."

    save_config(remote=remote, branch=branch)
    _notes_dir().mkdir(parents=True, exist_ok=True)

    error = _ensure_repo(remote, branch)
    if error is not None:
        return error

    _snapshot_meta()
    _git("add", "-A")

    committed = False
    if _git("diff", "--cached", "--quiet").returncode == 1:  # 1 == staged changes exist
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        commit = _git(
            "-c", "user.name=agentgrid", "-c", "user.email=agentgrid@localhost",
            "commit", "-m", f"notes sync {stamp}",
        )
        if commit.returncode != 0:
            return False, _trim("could not commit your notes", commit)
        committed = True

    pulled = False
    listing = _git("ls-remote", "--heads", "origin", branch)
    if listing.returncode == 0 and listing.stdout.strip():
        pull = _git("pull", "--rebase", "origin", branch)
        if pull.returncode != 0:
            _git("rebase", "--abort")
            return False, (
                "Local and remote notes conflict on the same lines. Your notes are safe "
                "and unchanged here; open the notes repo to merge them, then sync again."
            )
        pulled = True
        _restore_meta()

    push = _git("push", "origin", f"HEAD:{branch}")
    if push.returncode != 0:
        return False, _trim("could not push to the remote", push) + _auth_hint(push)

    save_config(remote=remote, branch=branch, last_sync=time.time(), last_status="ok")
    parts = []
    if committed:
        parts.append("saved local changes")
    if pulled:
        parts.append("pulled remote")
    parts.append("pushed")
    return True, "Synced — " + ", ".join(parts) + f" ({branch})."
