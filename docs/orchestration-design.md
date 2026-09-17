# Coordinator and provider architecture

Status: two things are built on this architecture, and they are deliberately
different tools.

**Workflows (Teams)** are authored: fixed steps, or a bounded coordinator that
delegates to named specialists in a saved definition. Order known in advance,
one run, serial execution.

**Orchestrators** are standing: an OpenRouter model with a tool menu that can
*create* agents rather than only route between predefined ones
(`agentgrid/orchestrator.py`, `agentgrid/orchestrator_run.py`). Implemented
from the list below: the credential store, the live model catalogue, streamed
text steps, the bounded coordinator, a tool-calling execution loop, per-run
dollar and step ceilings enforced before each call, durable state with resume
after a restart, and a human approval gate that suspends a run to disk rather
than holding a thread.

Still design work: parallel specialist execution (an orchestrator's *children*
run concurrently, but its own steps are serial), a pre-call cost reservation
rather than post-hoc accounting, nested delegation (an orchestrator cannot
create another orchestrator), and cross-machine state so two hosts share one
board.

## User experience

1. **Providers:** connect OpenRouter, test the connection, and browse its model
   catalog. Keep Claude Code and Codex CLI connections as separate choices.
2. **Agent definitions:** choose a provider/model, instructions, input contract,
   and permitted tools for each specialist.
3. **Workflow mode:** select Fixed steps or Coordinator. In Coordinator mode,
   independently choose the coordinator model, allowed specialists, run limits,
   and completion criteria.
4. **Run view:** show delegation decisions, active agents, complete outputs,
   revisions, token usage and available cost information. Keep Stop available.
5. **Export:** portable workflow definitions contain provider/model references,
   tool profiles and limits, never credentials or live run state.

A smaller coordinator can route to larger specialists. Its suitability should be
measured on correct delegation, recovery, and termination, not assumed from a
model's name. Fixed workflows remain preferable when the order is already known.

## Further runtime design

The coordinator receives tools such as `delegate(agent_id, task, input_refs)`,
`read_result(run_id)`, `request_revision(run_id, feedback)`, and `finish(result)`.
The backend validates every request against the workflow's agent allowlist and
run state. Model output never becomes an unchecked shell command.

A provider adapter normalizes streamed messages, usage, errors and tool calls.
Claude Code and Codex already supply their own execution environments. An
OpenRouter chat-completion adapter must run the requested client tools, append
the tool results to the conversation, and make the next model call. OpenRouter
supports this tool-calling protocol; model capabilities must be checked before
allowing a model to coordinate.

Keep shared state in AgentGrid: original brief, task IDs, parent/child relations,
artifact references, agent results, decisions and revision history. Bound context
by passing selected artifacts rather than repeatedly copying every conversation.
Persist run events so a server restart can recover state; do not blindly repeat
an interrupted tool with side effects.

The harness, independently of the model, enforces maximum delegation depth,
concurrency, calls, elapsed time, output tokens, and revisions. API cost limits
need a pre-call reservation based on input size and maximum output, reconciled
with reported usage. CLI subscriptions may not expose comparable dollar costs;
show those as unavailable rather than claiming an exact cross-provider budget.
Serialize file-writing agents sharing a checkout or give them isolated worktrees.

## Credentials

On macOS, store a user-supplied OpenRouter key in Keychain. An environment variable
is useful for non-desktop installations. The browser submits the key to the local
server once; subsequent reads expose only connection status. Never put keys in
localStorage, workflow JSON, prompts, URLs, logs, or exported artifacts. Child CLI
processes should not inherit the OpenRouter key when they do not need it.

API providers have their own usage/billing; they do not reuse a CLI subscription.
The UI should distinguish a Claude Code login from an Anthropic model accessed
through OpenRouter and list only models returned by the connected provider.

## Suggested implementation order

- OpenRouter credential store, connectivity check and live model catalog.
- OpenRouter text/review steps using the same workflow event vocabulary.
- Agent tool profiles and a bounded tool-calling execution loop.
- Coordinator mode using explicit named specialists and delegation tools.
- Durable state, recovery and coordinator evaluation scenarios before broader
  autonomous creation of agents or nested delegation.

The approval gate turned out to carry more weight than expected. Because a
pending decision is part of the persisted state rather than a blocked thread,
"ask me every time" costs nothing while it waits and survives a restart --
which is what makes it a usable default rather than a mode people switch off.
Auto mode still escalates the one action that puts a window in front of the
user, an interactive session, because the cost of that interruption is the
user's attention rather than tokens.

References: [OpenRouter API and model catalog](https://openrouter.ai/docs/quickstart),
[client tool calling](https://openrouter.ai/docs/guides/features/tool-calling).
