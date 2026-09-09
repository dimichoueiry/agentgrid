"""Drive Claude Code and Codex sessions headlessly and stream to the browser.

This is the chat panel's engine. It runs the real `claude` CLI in print mode
(`claude -p ... --output-format stream-json`) as a local subprocess in the
session's own repo, so every turn uses the same binary, the same tools, and --
crucially -- the *user's own Claude Code login*, exactly like the terminal. No
API key, no SDK, no framework: the CLI already is the agent, so we only drive it
and normalize what it prints. Codex sessions use `codex exec resume --json`
with their own login, native image attachments, and an explicit sandbox mode.

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
CODEX_BIN = "codex"

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


def with_attachments(message: str, attachments: list[str] | None) -> str:
    """Fold attachment file paths into the prompt text `claude -p` receives.

    Headless Claude Code has no image flag that is stable across releases; what
    it does reliably is read a local file whose absolute path appears in the
    prompt (its Read tool renders PNG/JPEG/GIF/WebP visually). So a pasted image
    is saved to disk and named here -- the path IS the attachment. Each path is
    on its own line, and an image with no words of its own still gets a nudge to
    look, so an image-only turn is not a bare list of paths.
    """
    paths = [str(p) for p in (attachments or []) if str(p).strip()]
    if not paths:
        return message
    listing = "\n".join(f"- {path}" for path in paths)
    body = (message or "").strip()
    label = "Attached image:" if len(paths) == 1 else "Attached images:"
    if body:
        return f"{body}\n\n{label}\n{listing}"
    return f"Please look at the attached image(s):\n{listing}"


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


def normalize_codex(raw: dict) -> list[dict]:
    """Translate documented `codex exec --json` events to the chat vocabulary."""
    kind = raw.get("type")
    if kind == "turn.started":
        return [{"type": "turn_started"}]
    if kind == "turn.completed":
        return [{"type": "turn_done", "ok": True, "stats": {"usage": raw.get("usage")}}]
    if kind in ("turn.failed", "error"):
        error = raw.get("error")
        message = error.get("message") if isinstance(error, dict) else raw.get("message")
        return [{"type": "error", "message": message or "Codex turn failed."}]
    item = raw.get("item")
    if not isinstance(item, dict):
        return []
    item_type = item.get("type")
    if kind == "item.completed" and item_type in ("agent_message", "reasoning"):
        text = item.get("text")
        return [{"type": "assistant_message" if item_type == "agent_message" else "thinking",
                 "text": text}] if isinstance(text, str) and text else []
    if item_type not in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
        return []
    if kind not in ("item.started", "item.completed"):
        return []
    name = {"command_execution": "Bash", "file_change": "Edit",
            "mcp_tool_call": item.get("tool") or "MCP", "web_search": "WebSearch"}[item_type]
    inputs = {"command": item.get("command")} if item_type == "command_execution" else item
    start = {"type": "tool_use", "id": item.get("id"), "name": name, "input": inputs}
    if kind == "item.started":
        return [start]
    result = {"type": "tool_result", "id": item.get("id"),
              "ok": item.get("status") != "failed" and item.get("exit_code") in (None, 0),
              "summary": _summarize(item.get("aggregated_output") or item.get("status") or "done")}
    # Changes and searches can arrive only as completed items.
    return [start, result] if item_type in ("file_change", "web_search") else [result]


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
    """Serial turns over one Claude Code or Codex session, streamed to N browser tabs.

    `session_id` is the id the CLI resumes with. It may start as None for a
    brand-new chat and is filled from the first `init` event, so the very next
    message resumes the conversation the CLI just created.
    """

    def __init__(self, session_id: str | None, cwd: str, engine: str = "claude") -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.engine = engine
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

    def send(self, message: str, posture: str, model: str = "",
             attachments: list[str] | None = None) -> None:
        """Queue a message; start the worker if it isn't already draining."""
        with self._lock:
            self._pending.put((message, posture, model, list(attachments or [])))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                try:
                    message, posture, model, attachments = self._pending.get_nowait()
                except queue.Empty:
                    self._worker = None
                    return
            self._run_turn(message, posture, model, attachments)

    def _run_turn(self, message: str, posture: str, model: str = "",
                  attachments: list[str] | None = None) -> None:
        if self.engine == "codex":
            argv = [CODEX_BIN, "exec", "-s",
                    "read-only" if posture == "read-only" else "workspace-write",
                    "-c", 'approval_policy="never"']
            if self.session_id:
                argv += ["resume"]
            argv += ["--json", "--skip-git-repo-check"]
            if model:
                argv += ["--model", model]
            for path in attachments or []:
                argv += ["--image", path]
            argv += ["--"]
            if self.session_id:
                argv += [self.session_id]
            argv += [message or "Please look at the attached image(s)."]
        else:
            prompt = with_attachments(message, attachments)
            argv = [CLAUDE_BIN, "-p", prompt,
                    "--output-format", "stream-json", "--verbose",
                    "--permission-mode", permission_mode(posture)]
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
            self._emit({"type": "error", "message": f"could not start {self.engine}: {error}"})
            return

        with self._lock:
            self._proc = proc
            self._running = True

        last_noise = ""
        finished = False
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
                if self.engine == "codex" and raw.get("type") == "thread.started":
                    if not self.session_id:
                        self.session_id = raw.get("thread_id")
                events = normalize_codex(raw) if self.engine == "codex" else normalize(raw)
                for event in events:
                    if event["type"] in ("turn_done", "error"):
                        finished = True
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
            if code not in (0, None) and not finished:
                self._emit({"type": "error",
                            "message": last_noise[:200] or f"{self.engine} exited {code}"})
            elif not finished:
                self._emit({"type": "error", "message": f"{self.engine.title()} exited without completing the turn."})

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

    def session(self, session_id: str, cwd: str, engine: str = "claude") -> ChatSession:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                existing = ChatSession(session_id, cwd, engine)
                self._sessions[session_id] = existing
            elif cwd and not existing.cwd:
                existing.cwd = cwd
            return existing

    def send(self, session_id: str, cwd: str, message: str, posture: str,
             model: str = "", attachments: list[str] | None = None,
             engine: str = "claude") -> None:
        self.session(session_id, cwd, engine).send(message, posture, model, attachments)

    def cancel(self, session_id: str) -> None:
        with self._lock:
            existing = self._sessions.get(session_id)
        if existing:
            existing.cancel()

    def state(self, session_id: str) -> dict:
        with self._lock:
            existing = self._sessions.get(session_id)
        return existing.state() if existing else {"running": False, "queued": 0}
