"""The ~/.agentgrid state directory, kept readable by its owner only.

Everything the app stores lives under one folder: system prompts, chat queues,
uploads, orchestrator runs, tickets, and web.json -- the file carrying the
board's access token. Created with the default umask it is world-readable
(0755), so any other local account could read all of it and walk in through
the token. Owner-only on the root is enough: without search permission on the
folder nobody else can reach anything below it, whatever each file's own mode.

Only the root is tightened, never a recursive walk: a walk would cost time on
every start with a large notes or uploads tree, and would change modes on files
the user may have deliberately shared out of it by symlink.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

OWNER_ONLY = 0o700


def state_dir() -> Path:
    """Resolved at call time, so a test (or a run) with another HOME is respected."""
    return Path.home() / ".agentgrid"


def secure_state_dir(path: Path | None = None) -> Path:
    """Create the state folder owner-only, or tighten an existing one to 0700.

    Never fatal: a folder we cannot fix still works, it is just no safer than
    before. A folder owned by someone else is left alone -- chmod would fail
    anyway, and it is not ours to change.
    """
    path = path or state_dir()
    try:
        path.mkdir(mode=OWNER_ONLY, parents=True, exist_ok=True)
        info = path.stat()
        if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) & 0o077):
            os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o077 | OWNER_ONLY)
    except OSError:
        pass
    return path
