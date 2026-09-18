"""What an orchestrator is told: its standing brief and its menu of tools.

Kept apart from `orchestrator_run` on purpose. That module is behaviour -- the
loop, the limits, the approval gate -- and this one is the wording and the
shape of what the model is offered, which is the part that gets rewritten most
often. Changing a description here cannot change what a tool is allowed to do:
the tools are executed in `orchestrator_run.Run`, and nothing in this file is
consulted when a call comes back.

The brief is assembled from a `Context` rather than from the posting alone,
because most of what makes an orchestrator useful is not the posting: it is
the persona (who it is, how it works, what it has learned), the team it may
call on (saved agents), what it knows about (skills and reusable prompts), and
what it has been told about this team. Menus list names and one-line
descriptions; full text is fetched on request, the way Claude Code loads
skills, so a well-equipped persona does not cost its whole library on every
call.

Every function takes the host's capabilities, because a menu that cannot
express an impossible request prevents one: a host with no scriptable
terminal does not offer an interactive session at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentgrid import orchestrator, personas, tickets

# One `wait_for_agents` call. Long enough to be useful for real work, short
# enough that a stop or a message from the user is answered promptly.
MAX_WAIT_SECONDS = 600
# A memory list is context for every call; past this it is summarised away.
MAX_MEMORY_IN_PROMPT = 40


@dataclass
class Context:
    """Everything the brief is built from, resolved once per step by the run."""

    posting: orchestrator.Orchestrator
    persona: personas.Persona
    capabilities: dict
    scope_name: str
    team: list[dict] = field(default_factory=list)      # saved agents it may start
    skills: list[dict] = field(default_factory=list)    # {name, description}, installed only
    prompts: list[dict] = field(default_factory=list)   # {name, description}, existing only
    posting_memory: list[dict] = field(default_factory=list)
    previous: dict | None = None                        # {goal, reason} of the last run


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required or [], "additionalProperties": False}}}


def tool_specs(context: Context) -> list[dict]:
    capabilities, persona, posting = context.capabilities, context.persona, context.posting
    defaults = persona.agent_defaults
    modes = ["background"] + (["interactive"] if capabilities.get("interactive") else [])
    engines = capabilities.get("engines") or ["claude"]
    shaped = (f"Leave engine, model and mode out to use your defaults "
              f"({_defaults_line(persona)}).")
    start_properties = {
        "project": {"type": "string", "description": "Absolute path from list_projects."},
        "task": {"type": "string", "description": "The full brief for this agent."},
        "mode": {"type": "string", "enum": modes},
        "engine": {"type": "string", "enum": engines},
        "model": {"type": "string", "description": (
            "Only to override your default. Allowed: " + ", ".join(persona.allowed_models) + "."
            if persona.allowed_models else "Only to override your default; an exact model ID.")},
        "name": {"type": "string", "description": "Short label shown on the board."},
        "systemPrompt": {"type": "string", "description": "Optional standing instructions."},
        "ticketKey": {"type": "string", "description": "Ticket to hand the agent, e.g. AG-12."},
    }
    if context.team:
        start_properties["savedAgent"] = {
            "type": "string", "enum": [agent["name"] for agent in context.team],
            "description": "Start one of your saved agents: its engine, model and standing "
                           "instructions are used as saved, and your task is its brief."}
    if not posting.scope:
        start_properties["areaId"] = {"type": "string", "description": "Work area for the new agent."}
    specs = [
        _tool("list_projects", "The directories you may start agents in. An agent can only "
              "be started in one of these.", {}),
        _tool("list_work_areas", "The work areas on the board, and which one you belong to.", {}),
        _tool("start_agent",
              "Start a real coding agent on this machine and put it on the board. "
              + shaped + " "
              + ("'interactive' opens a Terminal window the user can watch and type into. "
                 if capabilities.get("interactive") else
                 "This host cannot open interactive sessions, so background is the only mode. ")
              + "Prefer a saved agent from your team when one fits. Give one agent one clearly "
                "bounded job, and say in the task how it should report back and which of your "
                "skills it should use.",
              start_properties, ["project", "task"]),
        _tool("list_agents", "Every session on the board right now, with the ones you started "
              "marked `yours`. Check this before starting anything, so you do not duplicate work.", {}),
        _tool("read_agent_output", "The tail of what an agent has said and done. This is how you "
              "find out whether its work is actually finished.",
              {"agent": {"type": "string", "description": "Session id, or the name you gave it."},
               "chars": {"type": "integer", "description": "How much of the tail to read (default 6000)."}},
              ["agent"]),
        _tool("send_to_agent", "Send a follow-up instruction to an agent, as if you had typed it "
              "into its chat. Only reaches an agent that is not mid-turn: a message to a working "
              "agent is refused, because it would start a separate copy that agent never sees. "
              "You are woken when it finishes; send then.",
              {"agent": {"type": "string"}, "message": {"type": "string"}},
              ["agent", "message"]),
        _tool("wait_for_agents", "Wait until one of your agents finishes, gets blocked or stops, "
              "for at most the given time. Returns the moment something changes, with the tail of "
              "that agent's output, so you rarely need read_agent_output afterwards. Ending your "
              "turn with a progress note does the same wait for you, with no limit.",
              {"seconds": {"type": "integer", "description": f"1 to {MAX_WAIT_SECONDS}."}},
              ["seconds"]),
        _tool("list_tickets", "Read the ticket board.",
              {"status": {"type": "string", "description": "Comma separated: " + ", ".join(tickets.STATUSES) + "."},
               "area": {"type": "string"}, "project": {"type": "string"},
               "assignee": {"type": "string"}, "text": {"type": "string"},
               "limit": {"type": "integer"}}),
        _tool("create_ticket", "File a ticket, so work you decided on is visible to the user "
              "instead of living only in your head.",
              {"title": {"type": "string"}, "body": {"type": "string"},
               "type": {"type": "string", "enum": list(tickets.TYPES)},
               "priority": {"type": "string", "enum": list(tickets.PRIORITIES)},
               "status": {"type": "string", "enum": list(tickets.STATUSES)},
               "project": {"type": "string"}, "area": {"type": "string"}},
              ["title"]),
        _tool("move_ticket", "Move a ticket to another status.",
              {"ticket": {"type": "string"}, "status": {"type": "string", "enum": list(tickets.STATUSES)}},
              ["ticket", "status"]),
        _tool("comment_ticket", "Comment on a ticket.",
              {"ticket": {"type": "string"}, "text": {"type": "string"}}, ["ticket", "text"]),
        _tool("set_plan", "Your own working checklist, shown to the user in the Plan tab. Use it "
              "for steps too small to deserve a ticket.",
              {"items": {"type": "array", "items": {"type": "object", "properties": {
                  "text": {"type": "string"}, "done": {"type": "boolean"}},
                  "required": ["text"], "additionalProperties": False}}},
              ["items"]),
        _tool("remember", "Keep something for next time. The user sees what you saved and can "
              "undo it. Use 'persona' for how to work -- a preference or lesson that applies on "
              "every team you are posted to ('keep prompts short'). Use 'posting' for a fact about "
              "this team or product ('the SEO agent is retired'). One short sentence each.",
              {"text": {"type": "string"},
               "scope": {"type": "string", "enum": ["persona", "posting"]}},
              ["text", "scope"]),
        _tool("message_user", "Tell the user what changed. Do it whenever an agent finishes, gets "
              "blocked, or you start something new: they should never have to ask how it is going. "
              "They may not be watching, so this is a report, not a question you can wait on.",
              {"text": {"type": "string"}}, ["text"]),
        _tool("finish", "End this run. Only call it when the goal is met or cannot be met, and "
              "say which.", {"summary": {"type": "string"}}, ["summary"]),
    ]
    if context.skills:
        specs.append(_tool("read_skill", "Read one of your skills in full, before telling an agent "
                           "to use it or when you need its method yourself.",
                           {"name": {"type": "string", "enum": [s["name"] for s in context.skills]}},
                           ["name"]))
    if context.prompts:
        specs.append(_tool("read_prompt", "Read one of your reusable prompts in full, to follow it "
                           "or to hand it to an agent as its task.",
                           {"name": {"type": "string", "enum": [p["name"] for p in context.prompts]}},
                           ["name"]))
    if not posting.scope:
        specs.insert(2, _tool("create_work_area", "Create a work area on the board. You can do "
                              "this because you are the global orchestrator.",
                              {"name": {"type": "string"}}, ["name"]))
    return specs


def _defaults_line(persona: personas.Persona) -> str:
    defaults = persona.agent_defaults
    return f"{defaults.engine} · {defaults.model or 'the CLI default model'} · {defaults.mode}"


def _bullets(items: list[dict], render) -> list[str]:
    return [f"- {render(item)}" for item in items[-MAX_MEMORY_IN_PROMPT:]]


def system_prompt(context: Context) -> str:
    posting, persona, capabilities = context.posting, context.persona, context.capabilities
    limits = posting.limits
    approval = ("Starting an agent needs the user's approval every time: your request appears on "
                "their board and the run pauses until they answer, which may take hours. Ask for "
                "one agent at a time and say clearly what it is for."
                if posting.mode == "ask" else
                "You may start agents without asking, inside the limits below.")
    interactive = ("You may open interactive Terminal sessions."
                   if capabilities.get("interactive") else
                   "This host cannot open interactive Terminal sessions; every agent you start "
                   "runs in the background.")
    lines = [
        f"You are \"{persona.name}\", an orchestrator persona inside AgentGrid, the user's "
        f"dashboard over the coding agents running on their machine. This posting is "
        f"\"{posting.name}\"; your scope is {context.scope_name}.",
        "",
        "You have no filesystem, shell, editor or network access of your own. Everything you get "
        "done, you get done by starting real Claude Code or Codex agents in the user's projects "
        "and reading what they produce. Never claim work is done unless you read it in an agent's "
        "output or on a ticket.",
        approval,
        interactive,
    ]
    if persona.guidelines.strip():
        lines += ["", "## Who you are", persona.guidelines.strip()]
    lines += ["", "## How agents you start are shaped",
              f"Your defaults: {_defaults_line(persona)}. Leave engine, model and mode out and "
              f"these are used; AgentGrid fills them in."]
    if persona.allowed_models:
        lines.append("Agents may only run on: " + ", ".join(persona.allowed_models)
                     + ". Any other model is refused, so do not ask for one.")
    if context.team:
        lines += ["", "## Your team -- saved agents you can start with savedAgent",
                  *[f"- {a['name']} ({a['engine']}, {a.get('model') or 'default model'}): "
                    f"{_first_line(a.get('systemPrompt')) or 'no standing instructions'}"
                    for a in context.team],
                  "Prefer one of these to an improvised agent whenever the job fits."]
    if context.skills:
        lines += ["", "## Skills you know -- tell agents which to use; read_skill for the full text",
                  *[f"- {s['name']}: {s['description'][:240]}" for s in context.skills]]
    if context.prompts:
        lines += ["", "## Reusable prompts -- read_prompt to follow one or hand it to an agent",
                  *[f"- {p['name']}: {p['description']}" for p in context.prompts]]
    if persona.memory:
        lines += ["", "## What you have learned about how to work (applies on every team)",
                  *_bullets(persona.memory, lambda m: m["text"])]
    if posting.brief.strip():
        lines += ["", "## This team and product", posting.brief.strip()]
    if context.posting_memory:
        lines += ["", "## Facts you have been told about this team",
                  *_bullets(context.posting_memory, lambda m: m["text"])]
    if context.previous and context.previous.get("goal"):
        lines += ["", "## Your last run here",
                  f"Goal: {context.previous['goal'][:600]}",
                  f"Outcome: {(context.previous.get('reason') or 'no summary')[:600]}"]
    lines += [
        "",
        "## How to work",
        "- Look before you act: list_agents and list_tickets tell you what already exists.",
        "- One agent, one bounded job. Tell it how to report back and which skill to use.",
        "- After starting agents, end your turn with a one-line progress note. AgentGrid watches "
        "your agents for you, at no cost, and wakes you the moment one finishes, gets blocked or "
        "stops, with the tail of its output. Never poll.",
        "- When you are woken, check the work, tell the user what changed with message_user, and "
        "decide the next step. Follow up with send_to_agent rather than starting a duplicate.",
        "- The user should never have to ask how it is going: report every agent that finishes, "
        "gets blocked or fails, and every new one you start.",
        "- When the user corrects you or tells you something that will matter again, remember "
        "it -- as a persona lesson or a fact about this team -- so they never have to repeat it.",
        "- Use tickets for work worth seeing on the board, and set_plan for your own small steps.",
        "",
        "Ending a turn in plain text while your agents work is a progress note: the user sees it "
        "and you are woken when an agent changes. With nothing running, plain text hands the "
        "turn to the user and the run pauses until they reply -- do that only when you need "
        "their input. When the goal is met, call finish.",
        "",
        f"Limits enforced by AgentGrid, not by you: at most {limits.max_concurrent} agents running "
        f"at once, {limits.max_spawns} agents started per run, {limits.max_steps} steps, and "
        f"${limits.max_spend_usd:.2f} of your own model spend. When a limit is reached you will be "
        f"told in a tool result; finish with what you have and say what is left.",
    ]
    return "\n".join(lines).strip()


def _first_line(text: object) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""
