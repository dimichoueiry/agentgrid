"""Finding and focusing the terminal a session lives in.

This module answers one question — "where is this session's terminal, and can
I bring it to the front?" — using only `ps` and AppleScript. The capability
ceiling is structural: an interactive session's keyboard belongs to the pty
inside the emulator that owns it, and no other process can write to it, so
focusing the real tab is the most that can honestly be done. Anything more
would be pretending.

Only Terminal.app is scriptable enough to focus a tab by tty. Cursor and
VS Code keep their terminal tabs in renderer memory with no external
interface, so for those the honest behaviour is to say where the session
lives and why it cannot be focused, rather than guessing.

Everything here fails soft. A missing process, a vanished tab, a denied
automation permission — each returns a message for the caller to show, and
the UI keeps running. The one translation done for the user: macOS automation
errors are opaque (`-1743`), so they are turned into a pointer at the actual
setting that fixes them.
"""

from __future__ import annotations

import re
import subprocess

# Walks windows → tabs comparing each tab's tty. Each comparison is wrapped
# in `try` — a tab can vanish mid-walk, and one dead tab must not abort the
# search.
FOCUS_SCRIPT = """\
on run argv
  set wanted to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        try
          if (tty of t) is equal to wanted then
            set selected of t to true
            set index of w to 1
            activate
            return "ok"
          end if
        end try
      end repeat
    end repeat
  end tell
  return "notfound"
end run
"""

_HOST_NAMES = {
    "Apple_Terminal": "Terminal.app",
    "iTerm.app": "iTerm2",
    "vscode": "VS Code or Cursor",
}


def _run(argv: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess | None:
    """Run a subprocess, returning None on any failure."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def tty_of(pid: int) -> str | None:
    """Resolve a pid's controlling tty to a /dev path, or None.

    `ps` prints `??` for a process with no controlling terminal, which is a
    real answer meaning "nowhere to focus", not an error.
    """
    done = _run(["ps", "-o", "tty=", "-p", str(pid)])
    if done is None or done.returncode != 0:
        return None
    tty = (done.stdout or "").strip()
    if not tty or tty == "??":
        return None
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def host_of(pid: int) -> str | None:
    """Read TERM_PROGRAM out of a pid's environment, via `ps eww`.

    The environment is the only place the hosting emulator identifies itself;
    nothing about the tty says whether Terminal.app or Cursor owns it.
    """
    done = _run(["ps", "eww", "-o", "command=", "-p", str(pid)])
    if done is None or done.returncode != 0:
        return None
    match = re.search(r"\bTERM_PROGRAM=(\S+)", done.stdout or "")
    return match.group(1) if match else None


def describe_host(host: str | None) -> str:
    """A friendly name for a TERM_PROGRAM value."""
    if not host:
        return "an unknown terminal"
    return _HOST_NAMES.get(host, host)


def attached_tty(job_id: str) -> str | None:
    """Find the tty of an existing `claude attach <job>` process, if any.

    Matched on the process command line, not the tab title, so it survives
    renaming a session and cannot be fooled by two sessions sharing a name.
    Processes with no controlling terminal (`??`) are skipped — the daemon's
    own bg-pty-host helpers mention job ids too, and they live in no tab.
    """
    pattern = re.compile(rf"\battach\s+{re.escape(job_id)}\b")
    done = _run(["ps", "-Ao", "tty=,command="])
    if done is None or done.returncode != 0:
        return None
    for line in (done.stdout or "").splitlines():
        tty, _, command = line.strip().partition(" ")
        if not tty or tty == "??" or not pattern.search(command):
            continue
        return tty if tty.startswith("/dev/") else f"/dev/{tty}"
    return None


def _explain_osascript(stderr: str) -> str:
    """Translate opaque automation failures into the fix.

    Error -1743 ("not authorized") means macOS is blocking Terminal
    automation; the message points at the switch that turns it back on.
    """
    text = (stderr or "").strip()
    if "-1743" in text or "not authorized" in text.lower():
        return (
            "macOS is blocking Terminal automation. Allow it under "
            "System Settings → Privacy & Security → Automation."
        )
    return text[:200] or "osascript failed."


def focus_tty(tty: str) -> tuple[bool, str]:
    """Bring the Terminal.app tab owning `tty` to the front."""
    done = _run(["osascript", "-e", FOCUS_SCRIPT, tty], timeout=10.0)
    if done is None:
        return False, "Could not run osascript."
    if done.returncode != 0:
        return False, _explain_osascript(done.stderr)
    if (done.stdout or "").strip() == "ok":
        return True, "Focused the Terminal tab."
    return False, f"No Terminal.app tab owns {tty} — it may have closed."


def focus(pid: int) -> tuple[bool, str]:
    """Focus the terminal tab a pid lives in, or say exactly why not.

    Refuses non-Terminal.app hosts with a reason rather than guessing:
    Cursor's and VS Code's tabs have no external interface to focus, and a
    wrong guess would raise some unrelated window.
    """
    tty = tty_of(pid)
    if tty is None:
        return False, "That session has no controlling terminal to focus."
    host = host_of(pid)
    if host != "Apple_Terminal":
        return (
            False,
            f"This session lives in {describe_host(host)}, "
            "which cannot be focused from outside.",
        )
    return focus_tty(tty)
