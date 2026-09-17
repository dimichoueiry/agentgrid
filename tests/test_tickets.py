"""The ticket store, the `ag ticket` CLI, and the board's HTTP routes.

The behaviours worth pinning down are the ones two agents can break at once:
ids are never reused, a claim is one write, a bad id can never become a path,
and what the board reads back is what the CLI wrote.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentgrid import areas, tickets, ticket_cli, web


class TicketStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tickets, "TICKETS_DIR", Path(self.tmp.name) / "tickets")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_id_carries_the_project_and_numbers_climb_per_project(self):
        a = tickets.create("Fix the redirect", project="/repos/draw-cal")
        b = tickets.create("Pick a queue", project="/repos/draw-cal")
        c = tickets.create("Tidy the makefile", project="/repos/AgentGrid")
        d = tickets.create("Think about pricing")          # no project at all
        self.assertEqual([a["id"], b["id"], c["id"], d["id"]],
                         ["DC-1", "DC-2", "AG-1", "GEN-1"])
        # a multi-word name becomes initials, a single word its first letters
        self.assertEqual(tickets.project_key("/repos/draw-cal"), "DC")
        self.assertEqual(tickets.project_key("/repos/drawcal"), "DRAW")

    def test_renaming_a_prefix_renames_its_tickets_and_old_ids_still_resolve(self):
        tickets.create("One", project="/repos/cosmos")
        tickets.create("Two", project="/repos/cosmos")
        other = tickets.create("Else", project="/repos/draw-cal")
        result = tickets.rekey("/repos/cosmos", "sky")
        self.assertEqual(result, {"from": "COSM", "to": "SKY", "renamed": 2})
        self.assertEqual(sorted(t["id"] for t in tickets.all_tickets()),
                         [other["id"], "SKY-1", "SKY-2"])
        # numbering carries on, and an id from before the rename finds its ticket
        self.assertEqual(tickets.create("Three", project="/repos/cosmos")["id"], "SKY-3")
        self.assertEqual(tickets.get("COSM-2")["id"], "SKY-2")
        self.assertEqual(tickets.comment("cosm-1", "you", "still here")["id"], "SKY-1")
        self.assertEqual([p["key"] for p in tickets.board()["prefixes"]], ["SKY", "DC"])

    def test_a_prefix_cannot_collide_or_be_malformed(self):
        tickets.create("One", project="/repos/cosmos")
        tickets.create("Else", project="/repos/draw-cal")
        tickets.create("Loose")                                  # GEN-1
        for bad in ("DC", "GEN", "x", "toolong", "1AB", ""):
            with self.assertRaises(ValueError, msg=bad):
                tickets.rekey("/repos/cosmos", bad)
        with self.assertRaises(ValueError):
            tickets.rekey("/repos/never-filed", "NF")
        self.assertIsNotNone(tickets.load("COSM-1"))             # nothing moved

    def test_a_ticket_belongs_to_a_work_area_and_the_query_scopes_to_it(self):
        a = tickets.create("Landing page", area="marketing")
        tickets.create("Fix the redirect")
        self.assertEqual([t["id"] for t in tickets.query(area="marketing")], [a["id"]])
        moved = tickets.update(a["id"], {"area": ""}, who="you")
        self.assertEqual(moved["area"], "")
        self.assertEqual(moved["activity"][-1]["text"], "the work area")
        self.assertEqual(tickets.query(area="marketing"), [])

    def test_a_key_is_registered_once_and_never_moves(self):
        first = tickets.project_key("/repos/draw-cal")
        self.assertEqual(first, tickets.project_key("/repos/draw-cal"))
        # a second project wanting the same initials is given a distinct key
        other = tickets.project_key("/elsewhere/dinner-club")
        self.assertNotEqual(first, other)
        self.assertTrue(other.startswith("DC"))

    def test_a_number_is_not_reused_when_the_counter_is_lost(self):
        first = tickets.create("One", project="/repos/draw-cal")
        (tickets.TICKETS_DIR / tickets.META_NAME).unlink()      # counter gone
        second = tickets.create("Two", project="/repos/draw-cal")
        self.assertNotEqual(first["id"], second["id"])
        self.assertIsNotNone(tickets.load(first["id"]))         # the first survives

    def test_taking_a_ticket_assigns_and_starts_it_in_one_step(self):
        ticket = tickets.create("Fix the redirect", project="/repos/draw-cal")
        taken = tickets.take(ticket["id"], "sonnet-1", session_id="s-1")
        self.assertEqual((taken["assignee"], taken["status"], taken["sessionId"]),
                         ("sonnet-1", "in_progress", "s-1"))
        kinds = [entry["kind"] for entry in taken["activity"]]
        self.assertEqual(kinds, ["created", "assigned", "moved"])

    def test_someone_elses_running_ticket_is_not_taken_by_accident(self):
        ticket = tickets.create("Fix the redirect", project="/repos/draw-cal")
        tickets.take(ticket["id"], "sonnet-1")
        with self.assertRaises(ValueError) as caught:
            tickets.take(ticket["id"], "codex-2")
        self.assertIn("already in progress", str(caught.exception))
        # but an explicit assign still hands it over
        self.assertEqual(tickets.assign(ticket["id"], "codex-2")["assignee"], "codex-2")

    def test_done_stamps_a_closed_time_and_reopening_clears_it(self):
        ticket = tickets.create("Fix the redirect", project="/repos/draw-cal")
        self.assertTrue(tickets.move(ticket["id"], "done", who="you")["closed"])
        self.assertFalse(tickets.move(ticket["id"], "todo", who="you")["closed"])

    def test_status_takes_the_words_people_actually_type(self):
        ticket = tickets.create("Fix the redirect")
        for typed, stored in (("progress", "in_progress"), ("review", "review"),
                              ("in-review", "review"), ("finished", "done")):
            self.assertEqual(tickets.move(ticket["id"], typed)["status"], stored)
        with self.assertRaises(ValueError):
            tickets.move(ticket["id"], "nearly")

    def test_a_reorder_slots_a_card_between_its_neighbours(self):
        first = tickets.create("One", project="/repos/draw-cal")
        second = tickets.create("Two", project="/repos/draw-cal")
        third = tickets.create("Three", project="/repos/draw-cal")
        tickets.reorder(third["id"], "todo", before=second["id"])
        order = [t["id"] for t in tickets.all_tickets() if t["status"] == "todo"]
        self.assertEqual(order, [first["id"], third["id"], second["id"]])

    def test_an_id_can_never_become_a_path(self):
        for bad in ("../../etc/passwd", "DC-1/../../x", "", "not an id", "DC-"):
            with self.assertRaises(ValueError):
                tickets.ticket_path(bad)

    def test_a_hand_edited_file_still_reads_as_a_ticket(self):
        ticket = tickets.create("Fix the redirect", project="/repos/draw-cal")
        path = tickets.ticket_path(ticket["id"])
        path.write_text(json.dumps({"title": "Edited by hand", "status": "nonsense",
                                    "priority": "?", "due": "soon"}))
        recovered = tickets.get(ticket["id"])
        self.assertEqual((recovered["title"], recovered["status"], recovered["priority"],
                          recovered["due"]), ("Edited by hand", "todo", "medium", ""))

    def test_query_filters_stack_and_the_board_counts_what_exists(self):
        a = tickets.create("Fix the redirect", type="bug", project="/repos/draw-cal")
        tickets.create("Pick a queue", type="spike", project="/repos/draw-cal")
        tickets.create("Ship it", type="task", project="/repos/other")
        tickets.take(a["id"], "sonnet-1")
        tickets.move(tickets.create("Old", project="/repos/other")["id"], "done")
        self.assertEqual(len(tickets.query(project="/repos/draw-cal")), 2)
        self.assertEqual(len(tickets.query(type="bug,spike")), 2)
        self.assertEqual(len(tickets.query(assignee="sonnet-1")), 1)
        self.assertEqual(len(tickets.query(assignee="none", open_only=True)), 2)
        self.assertEqual(len(tickets.query(open_only=True)), 3)
        self.assertEqual(len(tickets.query(text="queue")), 1)
        board = tickets.board()
        self.assertEqual({p["name"]: p["open"] for p in board["projects"]},
                         {"draw-cal": 2, "other": 1})
        self.assertEqual([a["name"] for a in board["assignees"]], ["sonnet-1"])

    def test_the_handoff_tells_an_agent_how_to_report_back(self):
        ticket = tickets.create("Fix the redirect", body="The 302 drops the query.",
                                project="/repos/draw-cal", type="bug")
        tickets.comment(ticket["id"], "you", "Only on Safari.")
        brief = tickets.handoff_prompt(tickets.get(ticket["id"]), "sonnet-1")
        self.assertIn(ticket["id"], brief)
        self.assertIn("The 302 drops the query.", brief)
        self.assertIn("Only on Safari.", brief)
        self.assertIn(f"ag ticket move {ticket['id']} review", brief)

    def test_the_name_an_agent_files_under_comes_from_the_spawner(self):
        with mock.patch.dict("os.environ", {"AGENTGRID_AGENT": "review bot"}, clear=True):
            self.assertEqual(tickets.whoami(), "review bot")
        with mock.patch.dict("os.environ", {"CLAUDECODE": "1"}, clear=True):
            self.assertEqual(tickets.whoami(), "claude")
        with mock.patch.dict("os.environ", {"USER": "dimi"}, clear=True):
            self.assertEqual(tickets.whoami(), "dimi")


class TicketCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tickets, "TICKETS_DIR", Path(self.tmp.name) / "tickets")
        patcher.start()
        self.addCleanup(patcher.stop)
        here = mock.patch.object(ticket_cli, "project_of", lambda cwd="": "/repos/draw-cal")
        here.start()
        self.addCleanup(here.stop)

    def run_cli(self, *argv):
        from contextlib import redirect_stdout
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            code = ticket_cli.main(list(argv))
        return code, out.getvalue()

    def test_an_agent_files_claims_and_closes_a_ticket(self):
        code, out = self.run_cli("--as", "sonnet-1", "new", "Fix the redirect",
                                 "--type", "bug", "--priority", "high")
        self.assertEqual(code, 0)
        self.assertIn("DC-1", out)
        # the flag works on either side of the verb
        self.assertEqual(self.run_cli("take", "DC-1", "--as", "sonnet-1")[0], 0)
        self.assertEqual(tickets.get("DC-1")["assignee"], "sonnet-1")
        self.run_cli("--as", "sonnet-1", "comment", "DC-1", "Cookie SameSite.")
        self.run_cli("--as", "sonnet-1", "move", "DC-1", "review")
        self.assertEqual(tickets.get("DC-1")["status"], "review")
        self.assertEqual(self.run_cli("done", "DC-1")[0], 0)
        self.assertEqual(tickets.get("DC-1")["status"], "done")

    def test_area_takes_a_name_and_prefix_renames_this_projects_tickets(self):
        known = {"areas": [{"id": "a1", "name": "Marketing", "prompt": ""}], "members": {}}
        with mock.patch.object(areas, "load", lambda: known):
            code, _ = self.run_cli("new", "Landing page", "--area", "marketing")
            self.assertEqual((code, tickets.get("DC-1")["area"]), (0, "a1"))
            self.assertIn("area      Marketing", self.run_cli("show", "DC-1")[1])
            self.run_cli("new", "Elsewhere")
            self.assertIn("DC-1", self.run_cli("list", "--area", "Marketing")[1])
            self.assertNotIn("DC-2", self.run_cli("list", "--area", "Marketing")[1])
            self.assertEqual(self.run_cli("edit", "DC-1", "--area", "none")[0], 0)
            self.assertEqual(tickets.get("DC-1")["area"], "")
            self.assertEqual(self.run_cli("edit", "DC-1", "--area", "Nope")[0], 1)
        self.assertEqual(self.run_cli("prefix")[1].strip(), "DC")
        code, out = self.run_cli("prefix", "cal")
        self.assertEqual(code, 0)
        self.assertIn("DC is now CAL (2 renamed)", out)
        self.assertEqual(self.run_cli("show", "DC-1")[0], 0)      # the old id still works

    def test_a_bare_list_is_scoped_to_this_checkout_and_hides_nothing_silently(self):
        self.run_cli("new", "In this repo")
        tickets.create("Somewhere else", project="/repos/other")
        _, mine = self.run_cli("list")
        self.assertIn("DC-1", mine)
        self.assertNotIn("OTHE-1", mine)
        _, everywhere = self.run_cli("list", "--all")
        self.assertIn("OTHE-1", everywhere)

    def test_a_list_option_works_without_saying_list(self):
        # `ag tickets --mine` is the common shape; argparse exits on an unknown
        # top-level option, so the verb has to be implied before it parses.
        self.run_cli("--as", "sonnet-1", "new", "Mine", "--take")
        tickets.create("Someone else's", project="/repos/draw-cal", assignee="codex-2")
        for argv in (["--mine", "--as", "sonnet-1"], ["--as", "sonnet-1", "--mine"]):
            code, out = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("DC-1", out)
            self.assertNotIn("DC-2", out)
        self.assertIn("DC-2", self.run_cli("--all")[1])
        self.assertEqual({t["id"] for t in json.loads(self.run_cli("--json")[1])},
                         {"DC-1", "DC-2"})
        # and a real verb is still never mistaken for a list
        self.assertIn("Filed", self.run_cli("new", "A third")[1])

    def test_open_hides_done_and_json_is_machine_readable(self):
        self.run_cli("new", "One")
        self.run_cli("new", "Two")
        self.run_cli("done", "DC-2")
        _, open_only = self.run_cli("list", "--open")
        self.assertNotIn("DC-2", open_only)
        _, raw = self.run_cli("list", "--json")
        self.assertEqual({t["id"] for t in json.loads(raw)}, {"DC-1", "DC-2"})

    def test_show_prints_the_description_the_comments_and_the_history(self):
        self.run_cli("--as", "you", "new", "Fix the redirect", "--type", "bug",
                     "--body", "The 302 drops the query string.", "--due", "2026-09-18")
        self.run_cli("--as", "sonnet-1", "take", "DC-1")
        self.run_cli("--as", "sonnet-1", "comment", "DC-1", "Cookie SameSite.")
        self.run_cli("--as", "sonnet-1", "move", "DC-1", "review")
        code, out = self.run_cli("show", "DC-1")
        self.assertEqual(code, 0)
        self.assertIn("The 302 drops the query string.", out)
        self.assertIn("2026-09-18", out)
        self.assertIn("Cookie SameSite.", out)
        self.assertIn("In progress -> In review", out)
        self.assertIn("assigned it to sonnet-1", out)

    def test_an_unknown_ticket_is_an_error_not_a_traceback(self):
        code, _ = self.run_cli("show", "DC-99")
        self.assertEqual(code, 1)
        code, _ = self.run_cli("move", "nonsense", "done")
        self.assertEqual(code, 1)


def _serve(**over):
    session = SimpleNamespace(session_id="s-1", cwd="/repos/draw-cal", engine="claude",
                              status="idle", display_title="sonnet-1")
    session.__dict__.update(over)
    sent = []

    class Fleet:
        def snapshot(self):
            return {"sessions": [{"sessionId": "s-1", "title": "sonnet-1",
                                  "project": "draw-cal", "cwd": "/repos/draw-cal",
                                  "status": "idle", "engine": "claude"}]}

        def raw(self):
            return []

    handler = type("H", (web.Handler,), {
        "fleet": Fleet(), "token": "tok",
        "chat": SimpleNamespace(state=lambda *a: {},
                                send=lambda *a, **k: sent.append(a)),
    })
    handler._session_by_id = lambda self, sid: session if sid == "s-1" else None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], sent


def _call(port, path, body=None):
    url = f"http://127.0.0.1:{port}{path}?t=tok"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


class TicketRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tickets, "TICKETS_DIR", Path(self.tmp.name) / "tickets")
        patcher.start()
        self.addCleanup(patcher.stop)
        roots = mock.patch.object(web, "discover_projects", lambda sessions: [])
        roots.start()
        self.addCleanup(roots.stop)

    def test_the_board_arrives_with_its_sessions_and_every_reply_carries_it(self):
        port, _ = _serve()
        status, board = _call(port, "/api/tickets")
        self.assertEqual(status, 200)
        self.assertEqual(board["tickets"], [])
        self.assertEqual([s["title"] for s in board["sessions"]], ["sonnet-1"])
        status, created = _call(port, "/api/tickets/create",
                                {"title": "Fix the redirect", "type": "bug",
                                 "project": "/repos/draw-cal"})
        self.assertEqual(status, 200)
        self.assertEqual(created["ticket"]["id"], "DC-1")
        # the whole board rides on the reply, so the client never re-fetches
        self.assertEqual([t["id"] for t in created["board"]["tickets"]], ["DC-1"])

    def test_a_ticket_is_filed_under_a_real_work_area_or_refused(self):
        known = {"areas": [{"id": "a1", "name": "Marketing", "prompt": ""}], "members": {}}
        port, _ = _serve()
        with mock.patch.object(areas, "load", lambda: known):
            status, made = _call(port, "/api/tickets/create", {"title": "Landing page", "area": "a1"})
            self.assertEqual((status, made["ticket"]["area"]), (200, "a1"))
            status, reply = _call(port, "/api/tickets/update", {"id": "GEN-1", "area": "gone"})
            self.assertEqual(status, 400)
            status, reply = _call(port, "/api/tickets/update", {"id": "GEN-1", "area": ""})
            self.assertEqual((status, reply["ticket"]["area"]), (200, ""))

    def test_the_board_renames_a_prefix(self):
        port, _ = _serve()
        _call(port, "/api/tickets/create", {"title": "One", "project": "/repos/cosmos"})
        status, reply = _call(port, "/api/tickets/rekey", {"project": "/repos/cosmos", "key": "sky"})
        self.assertEqual((status, reply["from"], reply["to"]), (200, "COSM", "SKY"))
        self.assertEqual([t["id"] for t in reply["board"]["tickets"]], ["SKY-1"])
        status, reply = _call(port, "/api/tickets/rekey", {"project": "/repos/cosmos", "key": "!"})
        self.assertEqual(status, 400)

    def test_a_drag_moves_a_card_and_places_it(self):
        port, _ = _serve()
        for title in ("One", "Two", "Three"):
            _call(port, "/api/tickets/create", {"title": title, "project": "/repos/draw-cal"})
        status, moved = _call(port, "/api/tickets/move",
                              {"id": "DC-3", "status": "todo", "before": "DC-2"})
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in moved["board"]["tickets"]],
                         ["DC-1", "DC-3", "DC-2"])
        self.assertEqual(_call(port, "/api/tickets/move",
                               {"id": "DC-1", "status": "nowhere"})[0], 400)

    def test_assigning_to_a_live_session_tells_it_in_its_own_chat(self):
        port, sent = _serve()
        _call(port, "/api/tickets/create", {"title": "Fix the redirect",
                                            "project": "/repos/draw-cal"})
        status, body = _call(port, "/api/tickets/assign",
                             {"id": "DC-1", "sessionId": "s-1", "start": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["told"])
        self.assertEqual(body["ticket"]["assignee"], "sonnet-1")
        self.assertEqual(body["ticket"]["status"], "in_progress")
        self.assertIn("DC-1", sent[0][2])                   # the brief names the ticket
        self.assertIn("ag ticket", sent[0][2])              # and how to report back

    def test_a_name_with_no_session_is_a_label_and_nothing_is_sent(self):
        port, sent = _serve()
        _call(port, "/api/tickets/create", {"title": "Fix it", "project": "/repos/draw-cal"})
        status, body = _call(port, "/api/tickets/assign", {"id": "DC-1", "assignee": "a friend"})
        self.assertEqual((status, body["told"]), (200, False))
        self.assertEqual(sent, [])
        self.assertEqual(_call(port, "/api/tickets/assign",
                               {"id": "DC-1", "sessionId": "gone"})[0], 404)

    def test_bad_input_is_refused_rather_than_written(self):
        port, _ = _serve()
        self.assertEqual(_call(port, "/api/tickets/create", {"title": "  "})[0], 400)
        self.assertEqual(_call(port, "/api/tickets/update",
                               {"id": "../../etc/passwd", "title": "x"})[0], 400)
        _call(port, "/api/tickets/create", {"title": "Real", "project": "/repos/draw-cal"})
        self.assertEqual(_call(port, "/api/tickets/update", {"id": "DC-1"})[0], 400)
        self.assertEqual(_call(port, "/api/tickets/create",
                               {"title": "Bad date", "due": "soon"})[0], 400)
        self.assertEqual(_call(port, "/api/tickets/delete", {"id": "P-99"})[0], 404)

    def test_an_agent_started_from_a_ticket_gets_it_as_the_brief(self):
        port, _ = _serve()
        _call(port, "/api/tickets/create", {"title": "Fix the redirect",
                                            "project": "/repos/draw-cal",
                                            "body": "The 302 drops the query."})
        captured = {}

        def recorder(cwd, prompt, model, allowed, engine="claude", system_prompt="",
                     interactive=False, agent_name=""):
            captured.update(prompt=prompt, agent_name=agent_name)
            return True, "Started.", "job-1"

        with mock.patch.object(web, "spawn_agent", recorder), \
                mock.patch.object(web, "discover_projects",
                                  lambda s: [{"path": "/repos/draw-cal"}]):
            status, body = _call(port, "/api/spawn",
                                 {"cwd": "/repos/draw-cal", "ticketKey": "DC-1",
                                  "prompt": "also add a test"})
        self.assertEqual(status, 200)
        self.assertIn("DC-1", captured["prompt"])
        self.assertIn("The 302 drops the query.", captured["prompt"])
        self.assertIn("also add a test", captured["prompt"])
        # the agent is told its own board name, so `ag ticket` signs as that
        self.assertIn("DC-1", captured["agent_name"])
        self.assertEqual(tickets.get("DC-1")["status"], "in_progress")
        self.assertIn("DC-1 is in progress", body["message"])


class SpawnEnvironmentTests(unittest.TestCase):
    def test_the_board_name_reaches_the_agents_environment(self):
        self.assertEqual(web.agent_env("review bot")["AGENTGRID_AGENT"], "review bot")
        self.assertNotIn("AGENTGRID_AGENT", web.agent_env(""))
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "secret"}):
            self.assertNotIn("OPENROUTER_API_KEY", web.agent_env("bot"))


if __name__ == "__main__":
    unittest.main()
