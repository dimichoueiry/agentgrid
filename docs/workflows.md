# Creating workflows

Open **Teams** to start from a blank workflow, the writing example, the full
**Lesson pipeline** template, or imported JSON. Steps run from top to bottom.
Expand a step to edit it; use the arrows to reorder steps.

Each step has an engine (Claude or Codex), an optional exact model ID, permissions,
reusable agent instructions, and a task. Instructions accompany each invocation;
they are not installed as global CLI settings. Enable **Include original brief
and all earlier outputs** to pass that context automatically. For selective
handoffs, turn it off and use `{input}` and `{step_id}` in the task. Existing
workflows retain their manual handoffs when loaded.

The model suggestions come from local CLI caches. An empty model uses the
engine's configured default. A Codex step uses its workspace-write sandbox in
Auto mode and read-only sandbox in Read-only mode; Claude uses its existing
Auto/Read-only permission modes. Every step keeps its own conversation for
revision loops.

## From a conversation to a workflow

Ask an agent to design the workflow in ordinary chat. Under its reply, choose
**Export as workflow**. The reply opens in Teams' description box. Select the
engine/model for conversion, set an existing working directory, and choose
**Build draft**, then **Review draft**. Edit and save the draft. **Save & Run** is
a separate action that starts the actual workflow.

Descriptions are converted by a fresh, read-only Claude or Codex session. The
conversion does not continue or change the source conversation. Valid workflow
JSON can be loaded without a model call. Draft conversion has a three-minute
timeout and reports CLI/validation errors so the description can be edited and
retried. Closing the editor does not cancel an ongoing draft conversion.

## Reviews and results

A revision loop names the reviewer, a phrase to look for (for example
`REQUIRES REVISION`), the earlier step to revisit, and a maximum revision count.
The phrase is matched case-insensitively anywhere in the output. Have reviewers
use it only when requesting revisions. The feedback template can use `{at}` for
the review text. After the returning step, subsequent steps run again in order.

If the phrase remains after the revision limit, the run stops with an explicit
revision-limit result instead of reporting success. A failed step stops the run.
Stop clears the running agent's queued turns. A named workflow cannot run twice
at the same time.

Complete step outputs are shown in expandable panels, including outputs from
previous revisions. Copy a particular result or download the latest result for
each step as Markdown. Running/completed results can be reopened while the same
AgentGrid server is running; run history is currently held in memory.

## JSON interchange

**Export JSON** downloads the current editor definition after validation;
**Import JSON** opens a draft without saving or running it. Instructions, engine,
model, context sharing, and every revision loop are preserved.

```json
{
  "name": "Plan and write",
  "cwd": "/absolute/path/to/project",
  "nodes": [
    {
      "id": "plan",
      "role": "Planner",
      "engine": "claude",
      "model": "",
      "instructions": "Plan a clear explanation for the target reader.",
      "prompt": "Produce an outline.",
      "posture": "read-only",
      "includeContext": true
    },
    {
      "id": "write",
      "role": "Writer",
      "engine": "codex",
      "model": "",
      "instructions": "Explain mechanisms using concrete examples.",
      "prompt": "Write the article using the plan.",
      "posture": "read-only",
      "includeContext": true
    }
  ],
  "loops": []
}
```

IDs must be unique letters, digits, underscores or hyphens; `input` and `at` are
reserved. A loop must return to an earlier step. AgentGrid stores saved definitions
in `~/.agentgrid/teams/`. Definitions contain no API credentials.

OpenRouter and autonomous coordinator mode are proposed in
[the orchestration design](orchestration-design.md); they are not yet implemented.
