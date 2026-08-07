"""Drive a Claude Code session headlessly and stream it to the browser.

This is the chat panel's engine. It runs the real `claude` CLI in print mode
(`claude -p ... --output-format stream-json`) as a local subprocess in the
session's own repo, so every turn uses the same binary, the same tools, and --
crucially -- the *user's own Claude Code login*, exactly like the terminal. No
API key, no SDK, no framework: the CLI already is the agent, so we only drive it
and normalize what it prints.

Two things it guarantees:

- **One vocabulary out.** The CLI's raw `stream-json` is collapsed here into a
  small set of events (`turn_started`, `assistant_message`, `thinking`,
  `tool_use`, `tool_result`, `turn_done`, `error`). The browser codes against
  those alone, so a CLI change can't reach the UI.
- **A queue, not an interrupt.** A message sent while a turn is running is held
  and started when that turn finishes -- one turn per session at a time. That is
  the honest ceiling for the CLI (headless `-p` is one-shot); mid-turn injection
  would need the paid-API SDK, which we deliberately don't use.

Everything fails soft. A crashed subprocess, a torn JSON line, a vanished client
-- each is caught and, at most, turns into an `error` event; nothing here raises
into the server thread.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading

CLAUDE_BIN = "claude"

# The permission postures the UI offers, mapped to the CLI's --permission-mode.
# "auto" runs every tool without asking (the Stop button is the guard);
# "read-only" plans and answers without touching disk. Interactive per-tool
# approval is deliberately absent -- see the PRD.
POSTURES = {
    "auto": "bypassPermissions",
    "read-only": "plan",
}
DEFAULT_POSTURE = "auto"


def permission_mode(posture: str) -> str:
    return POSTURES.get(posture, POSTURES[DEFAULT_POSTURE])


# ---------------------------------------------------------------------------
# Normalizing the CLI's stream-json into the UI vocabulary.


def normalize(raw: dict) -> list[dict]:
    """One raw stream-json object → zero or more normalized UI events.

    Verified against the shapes the CLI (2.1.x) actually emits: a `system/init`
    opening the turn, `assistant` messages whose content is a list of
    text/thinking/tool_use blocks, `user` messages carrying tool_result blocks,
    and a final `result`. Anything else (hooks, rate-limit notices) is dropped
    rather than guessed at.
    """
    kind = raw.get("type")
    if kind == "system" and raw.get("subtype") == "init":
        return [{
            "type": "turn_started",
            "sessionId": raw.get("session_id"),
            "model": raw.get("model"),
            "cwd": raw.get("cwd"),
            # apiKeySource lets the UI show "on your subscription", not a key.
            "authSource": raw.get("apiKeySource"),
        }]
    if kind == "assistant":
        return _assistant_blocks(raw.get("message"))
    if kind == "user":
        return _tool_results(raw.get("message"))
    if kind == "result":
        return [{
            "type": "turn_done",
            "ok": not raw.get("is_error"),
            "result": raw.get("result"),
            "sessionId": raw.get("session_id"),
            "stats": {
                "durationMs": raw.get("duration_ms"),
                "numTurns": raw.get("num_turns"),
                "costUsd": raw.get("total_cost_usd"),
            },
        }]
    return []


def _assistant_blocks(message: object) -> list[dict]:
    if not isinstance(message, dict):
        return []
    events: list[dict] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                events.append({"type": "assistant_message", "text": str(text)})
        elif block_type == "thinking":
            text = block.get("thinking")
            if text:
                events.append({"type": "thinking", "text": str(text)})
        elif block_type == "tool_use":
            events.append({
                "type": "tool_use",
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input"),
            })
    return events


def _tool_results(message: object) -> list[dict]:
    if not isinstance(message, dict):
        return []
    events: list[dict] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        events.append({
            "type": "tool_result",
            "id": block.get("tool_use_id"),
            "ok": not block.get("is_error"),
            "summary": _summarize(block.get("content")),
        })
    return events


def _summarize(content: object) -> str:
    """A one-line gist of a tool result, for a compact card."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = ""
    return " ".join(text.split())[:140]


# ---------------------------------------------------------------------------
# One chattable session: a turn queue, a running subprocess, and SSE fan-out.


class ChatSession:
    """Serial turns over one Claude Code session, streamed to N browser tabs.

    `session_id` is the id the CLI resumes with. It may start as None for a
    brand-new chat and is filled from the first `init` event, so the very next
    message resumes the conversation the CLI just created.
    """

    def __init__(self, session_id: str | None, cwd: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self._pending: queue.Queue = queue.Queue()
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._worker: threading.Thread | None = None
        self._running = False

    # -- SSE fan-out --------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def _emit(self, event: dict) -> None:
        with self._lock:
            channels = list(self._subscribers)
        for channel in channels:
            channel.put(event)

    # -- turns --------------------------------------------------------------

    def send(self, message: str, posture: str, model: str = "") -> None:
        """Queue a message; start the worker if it isn't already draining."""
        self._pending.put((message, posture, model))
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                message, posture, model = self._pending.get_nowait()
            except queue.Empty:
                return
            self._run_turn(message, posture, model)

    def _run_turn(self, message: str, posture: str, model: str = "") -> None:
        argv = [
            CLAUDE_BIN, "-p", message,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", permission_mode(posture),
        ]
        # An explicit model overrides the CLI default for this turn only; empty
        # means "whatever your Claude Code is configured to use". Passed as its
        # own argv element (never a shell string), so it cannot inject.
        if model:
            argv += ["--model", model]
        if self.session_id:
            argv += ["--resume", self.session_id]
        try:
            proc = subprocess.Popen(
                argv,
                cwd=self.cwd or None,
                stdin=subprocess.DEVNULL,           # no tty -> no "waiting on stdin" warning
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,           # merged so a full stderr pipe can't deadlock
                text=True,
                bufsize=1,                          # line-buffered
                start_new_session=True,             # own process group, for a clean cancel
            )
        except (OSError, ValueError) as error:
            self._emit({"type": "error", "message": f"could not start claude: {error}"})
            return

        with self._lock:
            self._proc = proc
            self._running = True

        last_noise = ""
        try:
            for line in proc.stdout:                # blocks in this worker thread only
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    last_noise = line               # a warning/error line, kept for context
                    continue
                if not isinstance(raw, dict):
                    continue
                # Adopt the session id the CLI mints, so a new chat becomes
                # resumable and the next message continues it.
                if raw.get("type") == "system" and raw.get("subtype") == "init":
                    minted = raw.get("session_id")
                    if minted and not self.session_id:
                        self.session_id = minted
                for event in normalize(raw):
                    self._emit(event)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            code = proc.returncode
            with self._lock:
                self._proc = None
                self._running = False
            if code not in (0, None):
                self._emit({"type": "error",
                            "message": last_noise[:200] or f"claude exited {code}"})

    def cancel(self) -> None:
        """Stop the current turn and drop anything queued behind it."""
        with self._lock:
            proc = self._proc
        try:
            while True:
                self._pending.get_nowait()
        except queue.Empty:
            pass
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
        self._emit({"type": "turn_done", "ok": False, "result": None,
                    "cancelled": True, "stats": {}})

    def state(self) -> dict:
        with self._lock:
            return {"running": self._running, "queued": self._pending.qsize()}


# ---------------------------------------------------------------------------
# The registry the server holds: one ChatSession per session id.


class ChatManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._lock = threading.Lock()

    def session(self, session_id: str, cwd: str) -> ChatSession:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                existing = ChatSession(session_id, cwd)
                self._sessions[session_id] = existing
            elif cwd and not existing.cwd:
                existing.cwd = cwd
            return existing

    def send(self, session_id: str, cwd: str, message: str, posture: str,
             model: str = "") -> None:
        self.session(session_id, cwd).send(message, posture, model)

    def cancel(self, session_id: str) -> None:
        with self._lock:
            existing = self._sessions.get(session_id)
        if existing:
            existing.cancel()

    def state(self, session_id: str) -> dict:
        with self._lock:
            existing = self._sessions.get(session_id)
        return existing.state() if existing else {"running": False, "queued": 0}
