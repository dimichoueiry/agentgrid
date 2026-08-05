#!/usr/bin/env python3
"""Claude Code Notification / UserPromptSubmit hook for agentgrid.

Interactive sessions can only report `busy` or `idle`, so a session stopped
at a permission prompt is indistinguishable from one sitting idle. This hook
is the only source of that signal: it records who is waiting on a human in
~/.agentgrid/state.json, and agentgrid reads it to promote idle sessions to
"Needs you".

It is deliberately standalone -- it imports nothing from the package, because
it runs as its own process out of ~/.claude/settings.json and must work from
any checkout location. Two hard rules: it always exits 0 and never writes to
stdout, so a failure here can never block or disrupt the session it observes.
"""

import json
import os
import sys
import time
from pathlib import Path

STATE_PATH = Path.home() / ".agentgrid" / "state.json"
MAX_AGE_SECONDS = 7 * 24 * 3600


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    # The payload shape has varied; accept both snake_case and camelCase.
    session_id = payload.get("session_id") or payload.get("sessionId")
    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    notification = payload.get("notification_type") or payload.get("type") or ""
    if not session_id:
        return 0

    now = time.time()
    if notification == "agent_needs_input":
        entry = {"waiting": True, "since": now,
                 "reason": str(payload.get("message", ""))[:200]}
    elif notification == "agent_completed" or event == "UserPromptSubmit":
        entry = {"waiting": False, "since": now, "reason": ""}
    else:
        return 0

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    state[str(session_id)] = entry

    # Entries older than 7 days are dropped on each write, so the file cannot
    # grow without bound across months of sessions.
    kept = {}
    for key, value in state.items():
        try:
            if isinstance(value, dict) and now - float(value.get("since") or 0) < MAX_AGE_SECONDS:
                kept[key] = value
        except (TypeError, ValueError):
            continue

    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a concurrent reader must never see a half-written file.
        temp = STATE_PATH.with_suffix(".json.tmp")
        temp.write_text(json.dumps(kept, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, STATE_PATH)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
