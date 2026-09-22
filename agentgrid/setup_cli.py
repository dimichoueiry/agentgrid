"""`ag doctor` and `ag hook install` -- setup a person can run without the docs.

Doctor answers "why is my board empty / why is nothing amber" in one command;
hook install replaces the hand-merged JSON block in the README.

Three decisions shape it:

- **Every path is resolved from the home directory at call time**, not at
  import. The rest of the package freezes `Path.home()` into module constants,
  which is fine for a server but would make these commands untestable under a
  throwaway HOME -- and they are the ones that touch a file the user owns.
- **Hook install never destroys what it does not understand.** Settings that
  are not valid JSON, or whose `hooks` block has an unexpected shape, are
  reported and left alone. Unrelated keys, key order and the file's indent,
  mode and symlink all survive, and an already-installed hook is a no-op that
  does not touch the file at all.
- **Diagnostics print paths and versions, never contents.** settings.json can
  hold env secrets and web.json holds the board's token; neither is echoed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_EVENTS = ("Notification", "UserPromptSubmit")
HOOK_SCRIPT_NAME = "agentgrid_notify.py"
HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / HOOK_SCRIPT_NAME
DEFAULT_PORT = 8787
MIN_PYTHON = (3, 10)


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def state_dir() -> Path:
    return Path.home() / ".agentgrid"


def hook_command(script: Path = HOOK_SCRIPT) -> str:
    return f"python3 {shlex.quote(str(script))}"


def _tilde(path: Path | str) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


# --- hook ---------------------------------------------------------------------

class SettingsError(Exception):
    """settings.json exists but is not something this tool may rewrite."""


def _load_settings(path: Path) -> tuple[dict, str | None]:
    """The parsed settings and their raw text (None when the file is absent)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        raise SettingsError(f"cannot read {_tilde(path)}: {error.strerror}") from None
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except ValueError as error:
        raise SettingsError(f"{_tilde(path)} is not valid JSON "
                            f"(line {error.lineno}, column {error.colno})") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{_tilde(path)} is not a JSON object")
    return data, text


def _our_hooks(settings: dict, event: str) -> list[dict]:
    """The command entries under *event* that run the agentgrid hook script."""
    found = []
    hooks = settings.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else ():
        inner = group.get("hooks") if isinstance(group, dict) else None
        for entry in inner if isinstance(inner, list) else ():
            if isinstance(entry, dict) and HOOK_SCRIPT_NAME in str(entry.get("command", "")):
                found.append(entry)
    return found


def _script_in(command: str) -> Path | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for part in reversed(parts):
        if part.endswith(HOOK_SCRIPT_NAME):
            return Path(os.path.expanduser(part))
    return None


def merge_hook(settings: dict, command: str) -> list[str]:
    """Add or repoint the hook under each event, in place. Returns what changed."""
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError('"hooks" in settings.json is not an object')
    changes = []
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SettingsError(f'"hooks.{event}" in settings.json is not a list')
        ours = _our_hooks(settings, event)
        if not ours:
            groups.append({"hooks": [{"type": "command", "command": command}]})
            changes.append(f"added {event}")
        for entry in ours:
            # An existing entry pointing at an old checkout is repointed rather
            # than duplicated: two copies would record every event twice.
            if entry.get("command") != command:
                entry["command"] = command
                changes.append(f"updated {event}")
    return changes


def _indent_of(text: str | None) -> int | str:
    match = re.search(r"\n([ \t]+)\S", text or "")
    if not match:
        return 2
    return "\t" if match.group(1).startswith("\t") else len(match.group(1))


def _write_atomic(path: Path, text: str) -> None:
    """Replace *path* so no reader, Claude Code included, sees half a file.

    A symlinked settings.json (dotfile managers) is written through, not
    replaced by a regular file. The mode is carried over; a new file is
    owner-only, since settings can hold env secrets.
    """
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, temp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def install_hook(path: Path | None = None, script: Path = HOOK_SCRIPT) -> list[str]:
    """Install the hook into settings.json. Returns the changes; [] means none needed."""
    path = path or settings_path()
    if not script.is_file():
        raise SettingsError(f"hook script missing at {script}")
    settings, text = _load_settings(path)
    changes = merge_hook(settings, hook_command(script))
    if changes:
        body = json.dumps(settings, indent=_indent_of(text), ensure_ascii=False)
        ends = text is None or text.endswith("\n") or not text.strip()
        _write_atomic(path, body + ("\n" if ends else ""))
    return changes


# --- doctor ---------------------------------------------------------------------

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _version_of(binary: str) -> str:
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0].strip() if lines else ""


def check_python() -> tuple[str, str]:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] < MIN_PYTHON:
        return FAIL, (f"Python {version} is too old; agentgrid needs "
                      f"{'.'.join(map(str, MIN_PYTHON))}+")
    return OK, f"Python {version} ({sys.executable})"


def _find_codex() -> str | None:
    override = os.environ.get("AGENTGRID_CODEX_BIN")
    if override:
        return override if os.access(override, os.X_OK) else None
    from agentgrid import executables
    found = executables.codex_binary()
    return shutil.which(found) if not os.path.isabs(found) else found


def check_engines() -> list[tuple[str, str]]:
    claude = shutil.which("claude")
    codex = _find_codex()
    rows = []
    if claude:
        rows.append((OK, f"claude {_version_of(claude) or '(version unknown)'} at {_tilde(claude)}"))
    else:
        rows.append((WARN, "claude not found on PATH -- needed to chat with and start "
                           "Claude agents. Install Claude Code, then reopen the terminal."))
    if codex:
        rows.append((OK, f"codex {_version_of(codex) or '(version unknown)'} at {_tilde(codex)}"))
    else:
        rows.append((WARN, "codex not found -- only needed for Codex agents."))
    if not claude and not codex:
        rows.append((FAIL, "neither claude nor codex is installed; the board has nothing to show"))
    return rows


def check_state_dir() -> tuple[str, str]:
    folder = state_dir()
    if not folder.exists():
        if os.access(folder.parent, os.W_OK):
            return OK, f"state dir {_tilde(folder)} not created yet (made on first run)"
        return FAIL, f"state dir {_tilde(folder)} cannot be created: {_tilde(folder.parent)} is not writable"
    if not folder.is_dir():
        return FAIL, f"state dir {_tilde(folder)} exists but is not a directory"
    if not os.access(folder, os.W_OK | os.X_OK):
        return FAIL, f"state dir {_tilde(folder)} is not writable -- fix: chmod u+rwx {_tilde(folder)}"
    return OK, f"state dir {_tilde(folder)} is writable"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def check_web(port: int = DEFAULT_PORT) -> list[tuple[str, str]]:
    rows, ours = [], False
    record_path = state_dir() / "web.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    if isinstance(record, dict):
        # The file carries the board's token: it must stay owner-only, and the
        # token itself is never printed.
        mode = stat.S_IMODE(record_path.stat().st_mode)
        if mode & 0o077:
            rows.append((WARN, f"{_tilde(record_path)} is readable by others (mode {mode:o}) "
                               f"-- fix: chmod 600 {_tilde(record_path)}"))
        from agentgrid import web
        where = f"http://127.0.0.1:{record.get('port')}"
        if web.connection_answers(record):
            rows.append((OK, f"web board running at {where}"))
            ours = record.get("port") == port
        else:
            rows.append((WARN, f"{_tilde(record_path)} names {where}, which is not answering "
                               "(a run that did not exit cleanly; the next `ag --web` replaces it)"))
    if _port_free(port):
        rows.append((OK, f"port {port} is free for `ag --web`"))
    elif not ours:
        rows.append((WARN, f"port {port} is in use by something else; `ag --web` will pick "
                           "a free port, or pass --port"))
    return rows


def check_hook() -> list[tuple[str, str]]:
    path = settings_path()
    try:
        settings, text = _load_settings(path)
    except SettingsError as error:
        return [(WARN, f"hook status unknown: {error}")]
    if text is None:
        return [(WARN, f"hook not installed ({_tilde(path)} does not exist) -- run `ag hook install`")]
    rows = []
    missing = [event for event in HOOK_EVENTS if not _our_hooks(settings, event)]
    commands = {e.get("command", "") for event in HOOK_EVENTS for e in _our_hooks(settings, event)}
    if len(missing) == len(HOOK_EVENTS):
        return [(WARN, "hook not installed (optional: records why a session is waiting) "
                       "-- run `ag hook install`")]
    if missing:
        rows.append((WARN, f"hook missing for {', '.join(missing)} -- run `ag hook install`"))
    stale = [s for s in map(_script_in, commands) if s is None or not s.is_file()]
    if stale:
        rows.append((WARN, "hook points at a script that no longer exists -- run `ag hook install`"))
    if not rows:
        rows.append((OK, f"hook installed for {', '.join(HOOK_EVENTS)}"))
    return rows


def doctor(port: int = DEFAULT_PORT) -> int:
    rows = [check_python(), *check_engines(), check_state_dir(), *check_web(port), *check_hook()]
    print("agentgrid doctor\n")
    for status, message in rows:
        print(f"  {status:<5} {message}")
    warns = sum(status == WARN for status, _ in rows)
    fails = sum(status == FAIL for status, _ in rows)
    print(f"\n{fails} problem{'s' * (fails != 1)}, {warns} warning{'s' * (warns != 1)}.")
    return 1 if fails else 0


# --- entry points ---------------------------------------------------------------

def doctor_main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="ag doctor",
                                     description="Check that agentgrid can run here, and what to fix if not.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="web port to check (default 8787)")
    args = parser.parse_args(argv)
    return doctor(port=args.port)


def hook_main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="ag hook", description="Manage the Claude Code hook.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install", help=f"add the agentgrid hook to {_tilde(settings_path())} "
                                   "(safe to run again)")
    args = parser.parse_args(argv)
    if args.cmd == "install":
        try:
            changes = install_hook()
        except (SettingsError, OSError) as error:
            print(f"ag hook install: {error}. Nothing was changed.", file=sys.stderr)
            return 1
        where = _tilde(settings_path())
        if changes:
            print(f"Installed hook in {where}: {', '.join(changes)}.")
        else:
            print(f"Hook already installed in {where}; nothing to do.")
    return 0
