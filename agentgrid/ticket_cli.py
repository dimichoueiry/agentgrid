"""`ag ticket` -- the surface an agent uses to run its own work.

This exists because the board is a browser page and an agent has no browser.
Everything the web UI does to a ticket, one shell line does too, and both
write the same files -- otherwise agents would need a second, worse tracker.

Three decisions shape it:

- **Human-readable by default, `--json` on request.** An agent reads the plain
  listing perfectly well, and a person should not have to pipe through jq to
  see their own board.
- **Every command prints the ticket it changed**, in the same one-line form as
  the listing. An agent that just moved a ticket gets the new state back
  without a second call: one fewer round trip, one fewer way to be wrong.
- **`--project` defaults to the git checkout the command runs in.** An agent
  is started in a project and usually works from a subdirectory of it; filing
  from `draw-cal/src` and from `draw-cal` must land in the same place.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from agentgrid import tickets

# One character wide and unambiguous at a glance; type is what you scan a
# list for after the title. These match the glyphs the board draws.
GLYPH = {"bug": "●", "task": "■", "story": "◆", "spike": "▲", "chore": "○"}
SHORT = {"backlog": "backlog", "todo": "to do", "in_progress": "doing",
         "review": "review", "done": "done"}
PRIORITY_MARK = {"urgent": "!!", "high": "!", "medium": "", "low": "↓"}


def project_of(cwd: str = "") -> str:
    """The repository root containing *cwd*, else cwd itself."""
    here = Path(cwd or Path.cwd()).resolve()
    try:
        done = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                              capture_output=True, text=True, timeout=5)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(here)


def _resolve_project(value: str, none: bool) -> str:
    """"" means this checkout; "." and "here" say so explicitly; --no-project
    files a ticket that belongs to no codebase."""
    if none:
        return ""
    value = str(value or "").strip()
    if value in ("", ".", "here"):
        return project_of()
    return str(Path(value).expanduser())


def line(ticket: dict, width: int = 58) -> str:
    title = ticket["title"]
    if width and len(title) > width:
        title = title[:width - 1] + "…"
    mark = PRIORITY_MARK.get(ticket["priority"], "")
    due = f" due {ticket['due']}" if ticket["due"] and ticket["status"] != "done" else ""
    return (f"{ticket['id']:<9} {GLYPH.get(ticket['type'], '■')} "
            f"{SHORT[ticket['status']]:<8} {title}{(' ' + mark) if mark else ''}{due}"
            f"  · {ticket['projectName'] or '—'} · {ticket['assignee'] or 'unassigned'}")


def show(ticket: dict) -> str:
    out = [f"{ticket['id']}  {ticket['title']}",
           f"  {tickets.TYPE_LABELS[ticket['type']]} · "
           f"{tickets.STATUS_LABELS[ticket['status']]} · {ticket['priority']} priority",
           f"  project   {ticket['projectName'] or '—'}"
           f"{'  (' + ticket['project'] + ')' if ticket['project'] else ''}",
           f"  assignee  {ticket['assignee'] or 'nobody'}"]
    if ticket["reporter"]:
        out.append(f"  reporter  {ticket['reporter']}")
    if ticket["labels"]:
        out.append(f"  labels    {', '.join(ticket['labels'])}")
    if ticket["due"]:
        out.append(f"  due       {ticket['due']}")
    out.append(f"  created   {ticket['created'].replace('T', ' ')}")
    if ticket["closed"]:
        out.append(f"  closed    {ticket['closed'].replace('T', ' ')}")
    if ticket["body"]:
        out += [""] + ["  " + row for row in ticket["body"].splitlines()]
    # Comments first and in full: they are what an agent needs to pick this up.
    # The history is the audit trail, so it is last and trimmed.
    comments = [a for a in ticket["activity"] if a.get("kind") == "comment"]
    if comments:
        out += ["", "  Comments"]
        for note in comments:
            out.append(f"    {_when(note)}  {note.get('who')}: {note.get('text', '')}")
    history = [a for a in ticket["activity"] if a.get("kind") != "comment"]
    if history:
        out += ["", "  History"]
        for event in history[-12:]:
            out.append(f"    {_when(event)}  {event.get('who')} {_said(event)}")
    return "\n".join(out)


def _when(entry: dict) -> str:
    return str(entry.get("at", ""))[5:16].replace("T", " ")


def _said(entry: dict) -> str:
    kind = entry.get("kind")
    if kind == "moved":
        return (f"moved it {tickets.STATUS_LABELS.get(entry.get('from'), entry.get('from', ''))}"
                f" -> {tickets.STATUS_LABELS.get(entry.get('to'), entry.get('to', ''))}")
    if kind == "assigned":
        return f"assigned it to {entry.get('to') or 'nobody'}"
    if kind == "edited":
        return f"edited {entry.get('text', '')}"
    return "created it" if kind == "created" else str(entry.get("text") or kind or "")


def _listing(rows: list[dict], as_json: bool, everywhere: bool = False) -> int:
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No tickets match." if everywhere else
              "No tickets for this project. `ag ticket list --all` looks everywhere.")
        print('File one with: ag ticket new "…"')
        return 0
    for ticket in rows:
        print(line(ticket))
    open_count = sum(1 for t in rows if t["status"] != "done")
    print(f"\n{len(rows)} ticket{'' if len(rows) == 1 else 's'}, {open_count} open")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # --as and --json hang off every subcommand as well as the top level, so
    # both `ag ticket --json list` and `ag ticket list --json` work. They
    # default to SUPPRESS because a subparser writes its own defaults over
    # whatever the main parser already put in the namespace -- with a real
    # default, `ag ticket --as bot create ...` would silently lose the name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="actor", default=argparse.SUPPRESS, metavar="NAME",
                        help="the name to act under (default: $AGENTGRID_AGENT)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")

    parser = argparse.ArgumentParser(
        prog="ag ticket", parents=[common],
        description="File and run tickets on the agentgrid board. Agents and "
                    "the browser board share one store under ~/.agentgrid/tickets.")
    subs = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    add = subs.add_parser

    def sub(name, help_text, aliases=()):
        return add(name, help=help_text, parents=[common], aliases=aliases)

    ls = sub("list", "list tickets (the default command)", ("ls",))
    ls.add_argument("--project", default="", metavar="PATH",
                    help="PATH, a project name, or '.' for this checkout")
    ls.add_argument("--all-projects", action="store_true", dest="all_projects",
                    help="every project, not just this one")
    ls.add_argument("--status", default="", help=f"one or more of: {', '.join(tickets.STATUSES)}")
    ls.add_argument("--type", default="", help=f"one or more of: {', '.join(tickets.TYPES)}")
    ls.add_argument("--assignee", default="", metavar="NAME", help="a name, or 'none'")
    ls.add_argument("--label", default="")
    ls.add_argument("--text", default="", help="substring of id, title, body or labels")
    ls.add_argument("--mine", action="store_true", help="assigned to me")
    ls.add_argument("--open", action="store_true", dest="open_only",
                    help="hide done")
    ls.add_argument("--all", action="store_true", dest="all_statuses",
                    help="every project and every status")
    ls.add_argument("--limit", type=int, default=0)

    new = sub("new", "file a ticket", ("create", "add"))
    new.add_argument("title", nargs="+")
    new.add_argument("--type", default="task", choices=list(tickets.TYPES))
    new.add_argument("--status", default="todo", help=", ".join(tickets.STATUSES))
    new.add_argument("--priority", default="medium", choices=list(tickets.PRIORITIES))
    new.add_argument("--body", "--desc", "--description", dest="body", default="",
                     metavar="TEXT", help="'-' reads the description from stdin")
    new.add_argument("--due", default="", metavar="YYYY-MM-DD")
    new.add_argument("--project", default="", metavar="PATH")
    new.add_argument("--no-project", action="store_true", dest="no_project")
    new.add_argument("--label", action="append", default=[])
    new.add_argument("--assign", default="", metavar="NAME", help="'me' assigns it to you")
    new.add_argument("--take", action="store_true", help="assign it to me and start it")

    sub("show", "print one ticket in full", ("view",)).add_argument("key")
    sub("delete", "remove a ticket for good", ("rm",)).add_argument("key")

    sub("take", "claim a ticket and move it to In progress", ("pick", "claim")
        ).add_argument("key")

    mv = sub("move", "move a ticket to another column", ("mv",))
    mv.add_argument("key")
    mv.add_argument("status", help=", ".join(tickets.STATUSES))

    sub("done", "move a ticket to Done").add_argument("key")

    assign = sub("assign", "hand a ticket to someone ('none' clears it)")
    assign.add_argument("key")
    assign.add_argument("who", nargs="?", default="none")

    note = sub("comment", "add a note to a ticket", ("note",))
    note.add_argument("key")
    note.add_argument("text", nargs="+")

    edit = sub("edit", "change a ticket's fields")
    edit.add_argument("key")
    edit.add_argument("--title", default=None)
    edit.add_argument("--body", "--desc", "--description", dest="body", default=None,
                      help="'-' reads from stdin")
    edit.add_argument("--type", default=None, choices=list(tickets.TYPES))
    edit.add_argument("--priority", default=None, choices=list(tickets.PRIORITIES))
    edit.add_argument("--label", action="append", default=None,
                      help="repeatable; replaces the whole set")
    edit.add_argument("--project", default=None, metavar="PATH")
    edit.add_argument("--due", default=None, metavar="YYYY-MM-DD")

    sub("help", "print this help")
    return parser


def _text(value: str) -> str:
    return sys.stdin.read().strip() if value == "-" else value


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `ag tickets` with no verb means list. Re-parsing through the subparser
    # rather than defaulting cmd is what gives the namespace list's own
    # options; without it a bare `ag tickets` reaches for --mine and finds
    # nothing there.
    if not args.cmd:
        args = parser.parse_args(["list"] + list(argv))
    who = tickets.whoami(getattr(args, "actor", ""))
    as_json = bool(getattr(args, "json", False))
    cmd = args.cmd or "list"
    emit = lambda t, verb: print(json.dumps(t, indent=2) if as_json else f"{verb} {line(t)}")

    if cmd == "help":
        parser.print_help()
        return 0

    try:
        if cmd in ("list", "ls"):
            # Scoped to this checkout by default: an agent asking "what is
            # there to do" means here, and a fleet-wide list would bury it.
            everywhere = args.all_projects or args.all_statuses
            project = "" if everywhere else _resolve_project(args.project, False)
            rows = tickets.query(
                project=project, status=args.status, type=args.type,
                assignee=(who if args.mine else args.assignee),
                label=args.label, text=args.text,
                open_only=args.open_only, limit=args.limit)
            return _listing(rows, as_json, everywhere)

        if cmd in ("new", "create", "add"):
            ticket = tickets.create(
                " ".join(args.title), body=_text(args.body), type=args.type,
                status=args.status, priority=args.priority, due=args.due,
                project=_resolve_project(args.project, args.no_project),
                assignee=(who if (args.take or args.assign in ("me", "self")) else args.assign),
                reporter=who, labels=args.label)
            if args.take:
                ticket = tickets.take(ticket["id"], who)
            emit(ticket, "Filed")
            return 0

        if cmd in ("show", "view"):
            ticket = tickets.get(args.key)
            print(json.dumps(ticket, indent=2) if as_json else show(ticket))
            return 0

        if cmd in ("take", "pick", "claim"):
            emit(tickets.take(args.key, who), "Taken")
            return 0

        if cmd in ("move", "mv", "done"):
            status = "done" if cmd == "done" else args.status
            emit(tickets.move(args.key, status, who=who), "Moved")
            return 0

        if cmd == "assign":
            target = who if args.who in ("me", "self") else args.who
            if target in ("none", "nobody", "-"):
                target = ""
            emit(tickets.assign(args.key, target, who=who), "Assigned")
            return 0

        if cmd in ("comment", "note"):
            ticket = tickets.comment(args.key, who, " ".join(args.text))
            print(json.dumps(ticket, indent=2) if as_json
                  else f"Noted on {ticket['id']}.")
            return 0

        if cmd == "edit":
            changes = {}
            for field in ("title", "body", "type", "priority", "project", "due"):
                value = getattr(args, field)
                if value is not None:
                    changes[field] = _text(value) if field == "body" else value
            if args.label is not None:
                changes["labels"] = args.label
            if not changes:
                print("Nothing to change. Try --title/--body/--type/--priority/--label/--due.",
                      file=sys.stderr)
                return 1
            emit(tickets.update(args.key, changes, who=who), "Edited")
            return 0

        if cmd in ("delete", "rm"):
            key = str(args.key).strip().upper()
            tickets.get(key)                      # a bad id says so before unlinking
            tickets.delete(key)
            print(f"Deleted {key}.")
            return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    parser.print_help()
    return 1
