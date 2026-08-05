"""Transcript rendering: JSONL on disk to something a human can read.

Two renderers sit over the same parse, deliberately separate. `render_blocks`
returns typed dicts for the browser; `render_transcript` returns
`(text, colour_pair)` tuples for curses. Do not merge them, and do not serve
the curses renderer's output over the web API: that was the original bug —
the tuples were JSON-serialised into two-element arrays, and the browser's
`join()` stringified each one, so every line arrived ending in `,3`. A typed
renderer is also what lets the page style speakers differently and collapse
tool traffic.

Reading is always safe. The transcript is opened read-only, which makes it
safe on a session that is actively running and on one running in a terminal
this process cannot touch. An unreadable file renders as a single error block
rather than an exception — a transcript viewer that crashes on the file it
was asked to show is worse than one that says it could not.

One labelling subtlety: a `user` entry whose content blocks are all
`tool_result` is the harness feeding output back to the model, not a person
typing. It renders as results, never as a human turn — labelling machine
traffic "YOU" misattributes half the conversation.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

# Colour pair ids for the curses renderer. These line up with the palette in
# ui.py (§8.1 of the build spec): amber for the human, cyan for the model,
# the dim grey for tool traffic, the soft red for errors.
PAIR_PLAIN = 0
PAIR_YOU = 1
PAIR_CLAUDE = 2
PAIR_ERROR = 4
PAIR_DIM = 8

WRAP_COLUMNS = 100
GIST_LIMIT = 160
RESULT_LIMIT_WEB = 400
RESULT_LIMIT_TTY = 200

# Fields tried, in order, for a one-line gist of a tool call. The first
# present one is almost always the thing a human would ask "what did it do".
_GIST_FIELDS = (
    "command",
    "file_path",
    "pattern",
    "path",
    "query",
    "url",
    "description",
    "prompt",
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _summarize_tool_input(payload: object) -> str:
    """One line saying what a tool call was for.

    Falls back to compact JSON of the whole payload — an unfamiliar tool's
    input is still better shown raw than shown as nothing.
    """
    if isinstance(payload, dict):
        for field in _GIST_FIELDS:
            value = payload.get(field)
            if value:
                return _clip(str(value), GIST_LIMIT)
    try:
        raw = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = str(payload)
    return _clip(raw, GIST_LIMIT)


def _result_text(content: object) -> str:
    """Flatten a tool_result's content, which may be a string or blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _entry_blocks(entry: dict) -> list[dict]:
    """Turn one JSONL entry into zero or more display blocks."""
    entry_type = entry.get("type")
    if entry_type not in ("user", "assistant"):
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")

    if isinstance(content, str):
        text = content.strip()
        if not text:
            return []
        return [{"kind": entry_type, "text": text}]
    if not isinstance(content, list):
        return []

    dict_blocks = [block for block in content if isinstance(block, dict)]

    # The harness-feedback case: a user entry made entirely of tool results
    # is machine traffic. Render the results and never a "user" block.
    all_results = bool(dict_blocks) and all(
        block.get("type") == "tool_result" for block in dict_blocks
    )

    blocks: list[dict] = []
    for block in dict_blocks:
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if text and not all_results:
                blocks.append({"kind": entry_type, "text": text})
        elif block_type == "tool_use":
            blocks.append(
                {
                    "kind": "tool",
                    "name": str(block.get("name") or "tool"),
                    "gist": _summarize_tool_input(block.get("input")),
                }
            )
        elif block_type == "tool_result":
            blocks.append(
                {
                    "kind": "result",
                    "text": _clip(_result_text(block.get("content")), RESULT_LIMIT_WEB),
                    "error": bool(block.get("is_error")),
                }
            )
        elif block_type == "thinking":
            blocks.append({"kind": "thinking"})
    return blocks


def _is_codex_transcript(path: Path) -> bool:
    """Codex rollouts live under ~/.codex/sessions; the path is the format."""
    return ".codex" in path.parts


def _codex_entry_blocks(entry: dict) -> list[dict]:
    """Turn one codex rollout entry into zero or more display blocks.

    The rollout interleaves two streams: event_msg entries (the conversation
    as the TUI showed it) and response_item entries (the raw model turns,
    including a developer role full of harness instructions). The dialogue is
    read from event_msg — user_message is the clean human turn, agent_message
    the reply — and response_item supplies only the tool traffic, so harness
    noise is never labelled as a person.
    """
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return []
    entry_type = entry.get("type")

    if entry_type == "event_msg":
        event = payload.get("type")
        if event == "user_message":
            text = str(payload.get("message") or "").strip()
            return [{"kind": "user", "text": text}] if text else []
        if event == "agent_message":
            # Intermediate phases are progress narration; the final answer is
            # the reply worth reading in a dashboard.
            if payload.get("phase") in (None, "", "final_answer"):
                text = str(payload.get("message") or "").strip()
                return [{"kind": "assistant", "text": text}] if text else []
        return []

    if entry_type == "response_item":
        item = payload.get("type")
        if item == "function_call":
            try:
                arguments = json.loads(payload.get("arguments") or "{}")
            except ValueError:
                arguments = payload.get("arguments")
            return [
                {
                    "kind": "tool",
                    "name": str(payload.get("name") or "tool"),
                    "gist": _summarize_tool_input(arguments),
                }
            ]
        if item == "function_call_output":
            return [
                {
                    "kind": "result",
                    "text": _clip(str(payload.get("output") or ""), RESULT_LIMIT_WEB),
                    "error": False,
                }
            ]
        if item == "reasoning":
            return [{"kind": "thinking"}]
    return []


def render_blocks(path: Path) -> list[dict]:
    """Render a transcript as typed blocks for the browser, oldest first."""
    codex = _is_codex_transcript(path)
    blocks: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    blocks.extend(
                        _codex_entry_blocks(entry) if codex else _entry_blocks(entry)
                    )
    except OSError:
        # Fail as content, not as an exception: the panel shows one errored
        # result block and the app keeps running.
        return [{"kind": "result", "text": "Could not read the transcript.", "error": True}]
    return blocks


def _wrap(text: str, colour: int) -> list[tuple[str, int]]:
    lines: list[tuple[str, int]] = []
    for paragraph in str(text).splitlines() or [""]:
        wrapped = textwrap.wrap(
            paragraph,
            width=WRAP_COLUMNS,
            initial_indent="  ",
            subsequent_indent="  ",
        )
        if not wrapped:
            lines.append(("", colour))
        for piece in wrapped:
            lines.append((piece, colour))
    return lines


def render_transcript(path: Path) -> list[tuple[str, int]]:
    """Render a transcript as (text, colour_pair) lines for the curses grid."""
    speaker = "CODEX" if _is_codex_transcript(path) else "CLAUDE"
    lines: list[tuple[str, int]] = []
    for block in render_blocks(path):
        kind = block.get("kind")
        if kind == "user":
            lines.append(("YOU", PAIR_YOU))
            lines.extend(_wrap(block.get("text", ""), PAIR_PLAIN))
            lines.append(("", PAIR_PLAIN))
        elif kind == "assistant":
            lines.append((speaker, PAIR_CLAUDE))
            lines.extend(_wrap(block.get("text", ""), PAIR_PLAIN))
            lines.append(("", PAIR_PLAIN))
        elif kind == "tool":
            gist = block.get("gist", "")
            lines.extend(_wrap(f"[tool] {block.get('name', '')} {gist}".rstrip(), PAIR_DIM))
        elif kind == "result":
            text = _clip(block.get("text", ""), RESULT_LIMIT_TTY)
            if block.get("error"):
                lines.extend(_wrap(f"[error] {text}".rstrip(), PAIR_ERROR))
            else:
                lines.extend(_wrap(f"[result] {text}".rstrip(), PAIR_DIM))
        elif kind == "thinking":
            lines.extend(_wrap("[thinking]", PAIR_DIM))
    return lines
