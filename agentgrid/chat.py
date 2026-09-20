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
  would need the paid-API SDK, which we deliberately don't use. A held message
  can still be read, edited, reordered or removed until it starts, and a
  `queue` event tells every open tab when the queue changes.

Everything fails soft. A crashed subprocess, a torn JSON line, a vanished client
-- each is caught and, at most, turns into an `error` event; nothing here raises
into the server thread.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import signal
import subprocess
import threading

CLAUDE_BIN = "claude"
from agentgrid.executables import codex_binary
from agentgrid import discovery

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


def append_system_prompt_flags(text: str) -> list[str]:
    """The flags that append a session's standing prompt to one claude turn.

    `--append-system-prompt` keeps Claude Code's own identity, tools and safety
    guidance and only adds the board's instructions -- the right choice for a
    coding session, and, unlike a framed preamble, it never lands in the
    transcript as a message. It governs this invocation only, which is exactly
    the per-turn re-application "applies to future turns" asks for: every
    resumed turn passes the current stored prompt afresh.
    """
    text = (text or "").strip()
    return ["--append-system-prompt", text] if text else []


def frame_system_prompt(message: str, text: str) -> str:
    """A session's standing prompt folded into one codex turn's message.

    codex exec has no system-prompt flag that is stable across versions, so the
    instructions ride inside the turn as a framed preamble -- the same shape
    spawn uses at launch. Re-sent each turn so an edit reaches the model on the
    next turn; empty text leaves the message untouched.
    """
    text = (text or "").strip()
    if not text:
        return message
    return f"<system instructions>\n{text}\n</system instructions>\n\n{message}"


# Terminal colour and cursor codes, as codex's tracing lines carry them.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Codex 0.153+ allows one writer per thread: this is what `exec resume` says
# when an interactive `codex` (or another headless run) already holds it.
CODEX_THREAD_HELD = "already has an active writer"


def explain_exit(engine: str, noise: str, code: object) -> str:
    """One readable line for a turn whose CLI died without finishing it.

    The last thing the CLI printed that was not JSON is the best clue there
    is, but raw it is a tracing line with colour codes and an `Error:` prefix.
    The one failure with a known cause -- the thread is held elsewhere -- is
    said in terms of what to do about it rather than echoed.
    """
    text = ANSI_RE.sub("", noise or "").strip()
    if engine == "codex" and CODEX_THREAD_HELD in text:
        return ("Codex has this thread open somewhere else -- an interactive terminal "
                "or another codex run. Type there, or close it and send again.")
    if text.startswith("Error:"):
        text = text[len("Error:"):].strip()
    return text[:200] or f"{engine} exited {code}"


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_image_path(path: object) -> bool:
    return str(path).lower().endswith(IMAGE_SUFFIXES)


def with_attachments(message: str, attachments: list[str] | None) -> str:
    """Fold attachment file paths into the prompt text the agent receives.

    Headless Claude Code has no attachment flag that is stable across releases;
    what it does reliably is read a local file whose absolute path appears in
    the prompt (its Read tool renders images and PDFs, and reads text). So an
    attachment is saved to disk and named here -- the path IS the attachment.
    Each path is on its own line, and a file with no words of its own still
    gets a nudge to look, so a file-only turn is not a bare list of paths.
    """
    paths = [str(p) for p in (attachments or []) if str(p).strip()]
    if not paths:
        return message
    listing = "\n".join(f"- {path}" for path in paths)
    body = (message or "").strip()
    noun = "image" if all(is_image_path(p) for p in paths) else "file"
    label = f"Attached {noun}:" if len(paths) == 1 else f"Attached {noun}s:"
    if body:
        return f"{body}\n\n{label}\n{listing}"
    return f"Please look at the attached {noun}(s):\n{listing}"


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
        # Some CLI errors wrap the actual API error in a JSON message string.
        for _ in range(3):
            if not isinstance(message, str):
                break
            try:
                nested = json.loads(message)
            except ValueError:
                break
            if not isinstance(nested, dict):
                break
            inner = nested.get("error", nested)
            if not isinstance(inner, dict) or not isinstance(inner.get("message"), str):
                break
            message = inner["message"]
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

# Queue revisions count from zero in each server run, so a tab that outlived a
# restart would keep a revision the new run can never beat and freeze its queue.
# The epoch, minted once per process, travels with every revision; a tab forgets
# its revision when the epoch changes.
QUEUE_EPOCH = secrets.token_hex(4)


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
        self._api_history = []
        self._api_cancel = threading.Event()
        self._api_response = None
        self._cancel_generation = 0
        # Waiting turns, oldest first, each {id, message, posture, model,
        # attachments, generation, waited}. Guarded by _lock. `_current` is the
        # entry the worker holds from the moment it is taken until its turn ends.
        self._pending: list[dict] = []
        self._current: dict | None = None
        self._queue_seq = 0
        # A monotonic revision stamped on every copy of the queue sent to the
        # browser (a `queue` event or a /api/chat/state reply). It is bumped
        # under _lock in the same critical section that reads the queue, so the
        # revisions order snapshots by when they were taken -- even though they
        # are emitted outside the lock and so can overtake one another. A tab
        # applies a copy only if its revision beats the last one it applied, so
        # an older snapshot arriving late can no longer overwrite a newer queue.
        self._queue_rev = 0
        # Recently removed entries (id -> (index, entry)), so Remove can be undone.
        self._removed: dict[str, tuple[int, dict]] = {}
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
            self._emit_locked(event)

    def _emit_locked(self, event: dict) -> None:
        """Fan an event out to every subscriber. Caller holds _lock.

        put() on an unbounded Queue never blocks, so fanning out inside the
        lock cannot deadlock -- and it lets a queue change be announced in the
        very critical section that made it. No Stop or racing edit can slip
        between deciding to announce a change and announcing it, so a `started`
        can never trail a cancel (AG-10) and two changes always reach a tab in
        the order they were made (AG-11).
        """
        for channel in self._subscribers:
            channel.put(event)

    def _snapshot(self) -> tuple[int, list[dict]]:
        """The queue and a fresh revision for it. Caller holds _lock.

        Reading the queue and numbering that reading are one step, so two
        snapshots taken under the lock are always numbered in the order they
        were taken -- which is what lets a stale one be recognised later.
        """
        self._queue_rev += 1
        return self._queue_rev, self._queue_view()

    def _emit_queue(self, rev: int, snapshot: list[dict], started: dict | None = None) -> None:
        """Announce a queue snapshot to every tab. Caller holds _lock.

        The revision and the per-process epoch travel with it, so a tab drops a
        copy older than the one it shows and forgets its revision after a server
        restart (when the counter starts over).
        """
        self._emit_locked({"type": "queue", "queue": snapshot, "rev": rev,
                           "epoch": QUEUE_EPOCH, "started": started})

    # -- turns --------------------------------------------------------------

    def send(self, message: str, posture: str, model: str = "",
             attachments: list[str] | None = None) -> dict:
        """Queue a message; start the worker if it isn't already draining.

        Returns the entry's id and whether it waits behind other work. A
        message that waits is not shown as sent: it sits in the queue, where it
        can still be edited, and a `queue` event announces it when it starts.
        """
        with self._lock:
            self._queue_seq += 1
            entry = {"id": f"q{self._queue_seq}", "message": message, "posture": posture,
                     "model": model, "attachments": list(attachments or []),
                     "generation": self._cancel_generation,
                     "waited": self._current is not None or bool(self._pending)}
            self._pending.append(entry)
            self._ensure_worker()
            if entry["waited"]:
                rev, snapshot = self._snapshot()
                self._emit_queue(rev, snapshot)
        return {"id": entry["id"], "queued": entry["waited"]}

    def _ensure_worker(self) -> None:
        # Caller holds _lock.
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._drain, daemon=True)
            self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                self._current = None
                if not self._pending:
                    self._worker = None
                    return
                entry = self._pending.pop(0)
                if entry["generation"] != self._cancel_generation:
                    continue
                self._current = entry
                self._api_cancel.clear()
            if entry["waited"] and not self._announce_start(entry):
                # A Stop landed after this entry was taken but before its turn
                # was announced. cancel() has already emitted turn_done and an
                # empty queue, so drop the entry instead of announcing a start
                # that would relight "Working…" with no turn behind it (AG-10).
                continue
            self._run_turn(entry["message"], entry["posture"], entry["model"],
                           entry["attachments"])

    def _announce_start(self, entry: dict) -> bool:
        """Tell every tab that a waited message's turn is beginning.

        Emitted before turn_started so the browser shows the message first. The
        generation is re-checked under the lock: if a Stop slipped in between
        this entry being dequeued and here, its generation no longer matches
        and False is returned so the caller drops it rather than announcing a
        turn that will never run.
        """
        with self._lock:
            if entry["generation"] != self._cancel_generation:
                return False
            rev, snapshot = self._snapshot()
            self._emit_queue(rev, snapshot, started=self._public(entry))
        return True

    # -- the queue, as the browser sees and edits it ------------------------

    @staticmethod
    def _public(entry: dict) -> dict:
        return {"id": entry["id"], "message": entry["message"],
                "posture": entry["posture"], "model": entry["model"],
                "files": [os.path.basename(p) for p in entry["attachments"]]}

    def _queue_view(self) -> list[dict]:
        # Caller holds _lock. Only messages that waited are the queue: one sent
        # into an idle session sits in _pending for the instant before the
        # worker takes it, but it was shown as sent, not queued, so it is left
        # out -- otherwise a state() poll in that instant would show a phantom
        # entry no later event corrects.
        return [self._public(entry) for entry in self._pending if entry["waited"]]

    def _find(self, entry_id: str) -> int:
        # Caller holds _lock.
        for index, entry in enumerate(self._pending):
            if entry["id"] == entry_id:
                return index
        return -1

    def edit_queued(self, entry_id: str, message: str) -> bool:
        """Replace a waiting message's text. False once it has started or gone."""
        message = message.strip()
        with self._lock:
            index = self._find(entry_id)
            if index < 0:
                return False
            if not message and not self._pending[index]["attachments"]:
                raise ValueError("A queued message can't be empty. Remove it instead.")
            self._pending[index]["message"] = message
            # Snapshot and announce in the same critical section as the change,
            # so its revision orders it against any concurrent change and no
            # older snapshot can overtake it on the way to a tab.
            rev, snapshot = self._snapshot()
            self._emit_queue(rev, snapshot)
        return True

    def remove_queued(self, entry_id: str) -> bool:
        with self._lock:
            index = self._find(entry_id)
            if index < 0:
                return False
            self._removed[entry_id] = (index, self._pending.pop(index))
            while len(self._removed) > 20:
                self._removed.pop(next(iter(self._removed)))
            rev, snapshot = self._snapshot()
            self._emit_queue(rev, snapshot)
        return True

    def move_queued(self, entry_id: str, to: int) -> bool:
        with self._lock:
            index = self._find(entry_id)
            if index < 0:
                return False
            entry = self._pending.pop(index)
            self._pending.insert(max(0, min(int(to), len(self._pending))), entry)
            rev, snapshot = self._snapshot()
            self._emit_queue(rev, snapshot)
        return True

    def restore_queued(self, entry_id: str) -> bool:
        """Undo a Remove: put the entry back where it was, if Stop hasn't cleared it."""
        with self._lock:
            index, entry = self._removed.pop(entry_id, (-1, None))
            if entry is None or entry["generation"] != self._cancel_generation:
                return False
            # It may start at once if the queue drained meanwhile; either way
            # it was never shown as sent, so announce it when it starts.
            entry["waited"] = True
            self._pending.insert(min(index, len(self._pending)), entry)
            self._ensure_worker()
            rev, snapshot = self._snapshot()
            self._emit_queue(rev, snapshot)
        return True

    def _run_turn(self, message: str, posture: str, model: str = "",
                  attachments: list[str] | None = None) -> None:
        if self._api_cancel.is_set():
            # A Stop landed before this turn started. cancel() has already
            # emitted the cancelled turn_done, so drop the turn silently rather
            # than emit a second one.
            return
        if self.engine == "openrouter":
            from agentgrid import openrouter
            openrouter.run_turn(self, message, model, attachments)
            return
        # The session's standing system prompt, re-read every turn so an edit
        # made while it was running reaches this turn. Empty until a prompt is
        # set for this session (a brand-new chat has no id yet), so a plain turn
        # simply carries nothing extra. See discovery.save_system_prompt.
        standing = discovery.system_prompt_for(self.session_id)
        if self.engine == "codex":
            argv = [codex_binary(), "exec", "-s",
                    "read-only" if posture == "read-only" else "workspace-write",
                    "-c", 'approval_policy="never"']
            if self.session_id:
                argv += ["resume"]
            argv += ["--json", "--skip-git-repo-check"]
            if model:
                argv += ["--model", model]
            # --image only takes pictures; any other file is named in the
            # prompt, the same way the Claude path does it, for codex to open.
            images = [p for p in attachments or [] if is_image_path(p)]
            files = [p for p in attachments or [] if not is_image_path(p)]
            for path in images:
                argv += ["--image", path]
            argv += ["--"]
            if self.session_id:
                argv += [self.session_id]
            turn = with_attachments(message, files) or "Please look at the attached image(s)."
            argv += [frame_system_prompt(turn, standing)]
        else:
            prompt = with_attachments(message, attachments)
            argv = [CLAUDE_BIN, "-p", prompt,
                    "--output-format", "stream-json", "--verbose",
                    "--permission-mode", permission_mode(posture)]
            # Uploads live outside the project, and Read-only (plan mode) will
            # not open a file out there without a prompt nobody can answer
            # headlessly -- so grant the folder they sit in for this turn.
            for folder in sorted({os.path.dirname(p) for p in attachments or []}):
                argv += ["--add-dir", folder]
            if model:
                argv += ["--model", model]
            if self.session_id:
                argv += ["--resume", self.session_id]
            argv += append_system_prompt_flags(standing)
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
                env={k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"},
            )
        except (OSError, ValueError) as error:
            self._emit({"type": "error", "message": f"could not start {self.engine}: {error}"})
            return

        with self._lock:
            # A Stop can land while Popen is running: cancel() then finds no
            # _proc to kill and kills nothing, and without this the turn would
            # stream to completion after "Stopped". Re-check under the lock
            # before registering the process; if a Stop got in, kill it here.
            stop_during_launch = self._api_cancel.is_set()
            if not stop_during_launch:
                self._proc = proc
                self._running = True
        if stop_during_launch:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    pass
            return

        last_noise = ""
        hard_error = ""
        finished = False
        seen_errors = set()
        try:
            for line in proc.stdout:                # blocks in this worker thread only
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    # A warning/error line, kept for context. The CLI's own
                    # `Error:` verdict beats whatever tracing follows it.
                    last_noise = line
                    if ANSI_RE.sub("", line).startswith("Error"):
                        hard_error = line
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
                    if event["type"] == "error":
                        message_key = event.get("message", "")
                        if message_key in seen_errors:
                            continue
                        seen_errors.add(message_key)
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
                            "message": explain_exit(self.engine, hard_error or last_noise, code)})
            elif not finished:
                self._emit({"type": "error", "message": f"{self.engine.title()} exited without completing the turn."})

    def cancel(self) -> None:
        """Stop the current turn and drop anything queued behind it."""
        with self._lock:
            # Bumping the generation, setting the flag, emptying the queue and
            # announcing the end are one critical section. A message sent during
            # a Stop then either lands before it (older generation, dropped with
            # the rest) or after it (new generation, kept) -- never appended
            # between a bump and a clear in two separate locks and silently
            # lost. Announcing under the lock also keeps a `started` from
            # _announce_start from trailing the cancel out of order (AG-10).
            self._cancel_generation += 1
            self._api_cancel.set()
            proc = self._proc
            response = self._api_response
            self._pending.clear()
            self._removed.clear()
            rev, snapshot = self._snapshot()
            self._emit_locked({"type": "turn_done", "ok": False, "result": None,
                               "cancelled": True, "stats": {}})
            self._emit_queue(rev, snapshot)
        if response is not None:
            # Closing a blocked transport must not stall the Stop HTTP request.
            threading.Thread(target=response.close, daemon=True).start()
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass

    def state(self) -> dict:
        # A read, so it reports the current revision rather than minting one: a
        # poll that ties an in-flight `queue` event carries the same snapshot,
        # so there is nothing to reorder. (state() is polled often, once per
        # session on every sessions refresh -- it must not churn the counter.)
        with self._lock:
            snapshot = self._queue_view()
            return {"running": self._running, "queued": len(snapshot),
                    "queue": snapshot, "rev": self._queue_rev, "epoch": QUEUE_EPOCH}


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
             engine: str = "claude") -> dict:
        return self.session(session_id, cwd, engine).send(message, posture, model, attachments)

    def queue_action(self, session_id: str, action: str, entry_id: str,
                     message: object = None, index: object = None) -> bool:
        """Edit, remove, move or restore one waiting message.

        False means the entry is no longer waiting (it started, or Stop cleared
        it). ValueError means the request itself is wrong.
        """
        with self._lock:
            existing = self._sessions.get(session_id)
        if existing is None:
            return False
        if action == "edit":
            return existing.edit_queued(entry_id, str(message or ""))
        if action == "remove":
            return existing.remove_queued(entry_id)
        if action == "restore":
            return existing.restore_queued(entry_id)
        if action == "move":
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("Say where to move it.")
            return existing.move_queued(entry_id, index)
        raise ValueError(f"Unknown queue action: {action}.")

    def cancel(self, session_id: str) -> None:
        with self._lock:
            existing = self._sessions.get(session_id)
        if existing:
            existing.cancel()

    def state(self, session_id: str) -> dict:
        with self._lock:
            existing = self._sessions.get(session_id)
        return existing.state() if existing else {"running": False, "queued": 0, "queue": []}
