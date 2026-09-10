"""Resolve installed Codex versions consistently for chat and new agents."""
import os
from pathlib import Path
import re
import shutil
import subprocess
from functools import lru_cache


def _version(path):
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=3)
        match = re.search(r"codex-cli (\d+)\.(\d+)\.(\d+)", result.stdout)
        return tuple(map(int, match.groups())) if match else (0, 0, 0)
    except (OSError, subprocess.SubprocessError):
        return (0, 0, 0)


@lru_cache(maxsize=1)
def codex_binary():
    override = os.environ.get("AGENTGRID_CODEX_BIN")
    if override:
        return override
    candidates = [shutil.which("codex")]
    for app in (Path("/Applications/Codex.app"), Path.home() / "Applications/Codex.app"):
        binary = app / "Contents/Resources/codex"
        if binary.is_file() and os.access(binary, os.X_OK):
            candidates.append(str(binary))
    candidates = list(dict.fromkeys(p for p in candidates if p))
    return max(candidates, key=_version) if candidates else "codex"
