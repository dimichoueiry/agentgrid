"""Command-line entry point: `ag` for the terminal grid, `ag --web` for the
browser board, `ag ticket ...` for the ticket board agents drive from a shell.

The two front ends are interchangeable views over the same data model, so the
dispatch here is the whole of what this module does. The one structural
decision: `agentgrid.web` is imported lazily inside the web branch, so the
curses path never pays for the HTTP machinery -- and the layout tests can stub
`curses` without dragging in `http.server`.
"""

from __future__ import annotations

import argparse

FILTERS = ("all", "active", "needs you", "recent")

# `ag ticket new "..."` has to work, and argparse cannot express "a
# subcommand, but also a bare `ag` with only flags". The verb is therefore
# matched before the parser is built and handed off whole -- which also keeps
# the ticket CLI's own --help from being tangled with the grid's flags.
TICKET_VERBS = ("ticket", "tickets")


def main(argv: list[str] | None = None) -> None:
    import sys
    argv = sys.argv[1:] if argv is None else list(argv)
    # Before any front end touches it: the state folder holds the board's
    # token and every stored prompt, so it is owner-only on every entry point.
    from agentgrid import state
    state.secure_state_dir()
    if argv and argv[0] in TICKET_VERBS:
        # Imported here for the same reason web is: the grid must not pay for
        # a front end it is not showing.
        from agentgrid import ticket_cli
        raise SystemExit(ticket_cli.main(argv[1:]))

    parser = argparse.ArgumentParser(
        prog="ag",
        description="A local dashboard over every Claude Code session on this machine.",
    )
    parser.add_argument("--web", action="store_true",
                        help="serve the browser board instead of the terminal grid")
    parser.add_argument("--filter", default="all", metavar="NAME",
                        help="grid filter: all | active | needs-you | recent")
    parser.add_argument("--cwd", default=None, metavar="PATH",
                        help="only sessions under this directory")
    parser.add_argument("--no-mouse", action="store_true", dest="no_mouse",
                        help="keep the terminal's own text selection")
    parser.add_argument("--refresh", type=float, default=2.0, metavar="SECONDS",
                        help="poll interval in seconds (default 2)")
    parser.add_argument("--port", type=int, default=8787,
                        help="web port (falls back to a free one if taken)")
    parser.add_argument("--no-open", action="store_true", dest="no_open",
                        help="do not launch a browser")
    parser.add_argument("--root", action="append", default=None, metavar="DIR",
                        help="scan DIR for projects instead of the defaults (repeatable)")
    parser.epilog = ("Tickets: `ag tickets` lists the board; "
                     "`ag ticket --help` shows the rest.")
    args = parser.parse_args(argv)

    # Accept both spellings of the two-word filter; the grid sees one.
    args.filter = args.filter.replace("-", " ").strip().lower()
    if args.filter not in FILTERS:
        parser.error(f"--filter must be one of: {', '.join(FILTERS)}")

    if args.web:
        # Lazy on purpose: the curses path must never pay for the HTTP
        # machinery, and the layout tests stub curses without http.server.
        from agentgrid import web
        web.serve(port=args.port, open_browser=not args.no_open, roots=args.root)
    else:
        from agentgrid import ui
        ui.main(args)


if __name__ == "__main__":
    main()
