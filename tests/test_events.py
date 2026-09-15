"""Session status changes as events: the hub that notices them and the SSE
route (`GET /api/events`) that hands them to a companion such as Chief of Staff.
"""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from agentgrid import web


def _session(session_id="s1", status="working", **over):
    base = {"sessionId": session_id, "title": "Refactor auth", "engine": "claude",
            "cwd": "/Users/me/Desktop/drawcal", "project": "drawcal"}
    base.update(over)
    base["status"] = status
    return base


class EventHubTests(unittest.TestCase):
    def test_a_status_change_is_one_event_and_the_first_look_only_records(self):
        hub = web.EventHub()
        self.assertEqual(hub.observe([_session()]), [])
        self.assertEqual(hub.observe([_session()]), [])
        events = hub.observe([_session(status="done")])
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["id"], f"{hub.run}-1")
        self.assertEqual((event["type"], event["sessionId"], event["title"], event["from"], event["to"]),
                         ("status", "s1", "Refactor auth", "working", "done"))
        self.assertTrue(event["finished"])
        self.assertEqual(hub.observe([_session(status="done")]), [])  # seen again: nothing new

    def test_finished_means_working_then_settled(self):
        hub = web.EventHub()
        hub.observe([_session("a", "working"), _session("b", "idle"), _session("c", "working")])
        events = hub.observe([_session("a", "blocked"), _session("b", "working"), _session("c", "unknown")])
        by_id = {e["sessionId"]: e for e in events}
        self.assertTrue(by_id["a"]["finished"])
        self.assertFalse(by_id["b"]["finished"])   # idle -> working: it started
        self.assertFalse(by_id["c"]["finished"])   # working -> unknown: not settled

    def test_sessions_that_appear_or_vanish_are_not_events(self):
        hub = web.EventHub()
        hub.observe([_session("a")])
        self.assertEqual(hub.observe([_session("a"), _session("b", "done")]), [])
        self.assertEqual(hub.observe([_session("b", "done")]), [])
        # b was recorded when it appeared, so its next change counts.
        self.assertEqual([e["to"] for e in hub.observe([_session("b", "working")])], ["working"])

    def test_subscribers_get_events_as_they_happen_and_replay_from_their_last_id(self):
        hub = web.EventHub()
        hub.observe([_session("a"), _session("b")])
        live = hub.subscribe()
        hub.observe([_session("a", "done"), _session("b")])
        hub.observe([_session("a", "done"), _session("b", "failed")])
        self.assertEqual([live.get_nowait()["sessionId"] for _ in range(2)], ["a", "b"])
        self.assertTrue(live.empty())

        late = hub.subscribe(f"{hub.run}-1")
        self.assertEqual(late.get_nowait()["id"], f"{hub.run}-2")
        self.assertTrue(late.empty())
        stale = hub.subscribe("oldrun-1")   # another run's id: nothing to replay
        self.assertTrue(stale.empty())
        fresh = hub.subscribe("")
        self.assertTrue(fresh.empty())

        hub.unsubscribe(live)
        hub.observe([_session("a", "working"), _session("b", "failed")])
        self.assertTrue(live.empty())
        self.assertEqual(late.get_nowait()["to"], "working")

    def test_the_replay_log_is_bounded(self):
        hub = web.EventHub()
        hub.observe([_session()])
        for i in range(web.EventHub.KEEP + 20):
            hub.observe([_session(status="done" if i % 2 == 0 else "working")])
        channel = hub.subscribe(f"{hub.run}-1")
        self.assertEqual(channel.qsize(), web.EventHub.KEEP)


class FleetAnnounceTests(unittest.TestCase):
    def test_each_poll_is_shown_to_the_hub_with_the_overlay_applied(self):
        fleet = web.Fleet()
        fleet._as_json = lambda session: dict(session)  # sessions here are plain dicts
        fleet.overlay = lambda sessions: [dict(s, status="working") if s["sessionId"] == "chat" else s for s in sessions]
        channel = fleet.events.subscribe()
        fleet._announce([_session("chat", "idle"), _session("bg", "working")])
        fleet._announce([_session("chat", "idle"), _session("bg", "done")])
        self.assertEqual([(e["sessionId"], e["from"], e["to"]) for e in [channel.get_nowait()]], [("bg", "working", "done")])
        self.assertTrue(channel.empty())  # chat stayed "working" through the overlay
        fleet.overlay = lambda sessions: sessions
        fleet._announce([_session("chat", "idle"), _session("bg", "done")])
        self.assertEqual([(e["sessionId"], e["to"]) for e in [channel.get_nowait()]], [("chat", "idle")])
        self.assertTrue(channel.empty())

    def test_a_fault_in_the_hub_never_stops_polling(self):
        fleet = web.Fleet()
        fleet.overlay = lambda sessions: 1 / 0
        fleet._announce([_session()])  # no raise


class EventsRouteTests(unittest.TestCase):
    def setUp(self):
        self.hub = web.EventHub()
        fleet = SimpleNamespace(events=self.hub, snapshot=lambda: {"sessions": [], "polledAt": 0.0, "error": None})
        bound = type("BoundHandler", (web.Handler,),
                     {"fleet": fleet, "token": "tok", "chat": None, "log_message": lambda *a, **k: None})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _open(self, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events", headers={"X-Agentgrid-Token": "tok", **(headers or {})})
        return conn, conn.getresponse()

    def _frame(self, resp):
        """One SSE frame: the lines up to a blank one."""
        lines = []
        while True:
            line = resp.fp.readline().decode("utf-8").rstrip("\n")
            if line == "":
                if lines:
                    return lines
                continue
            lines.append(line)

    def test_needs_the_token(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events")
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_streams_status_changes_with_ids_and_replays_after_last_event_id(self):
        self.hub.observe([_session()])
        conn, resp = self._open()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.headers["Content-Type"].startswith("text/event-stream"))
        self.assertEqual(self._frame(resp), ["retry: 3000", f": connected {self.hub.run}"])
        self.hub.observe([_session(status="done")])
        frame = self._frame(resp)
        self.assertEqual(frame[0], f"id: {self.hub.run}-1")
        event = json.loads(frame[1].removeprefix("data: "))
        self.assertEqual((event["sessionId"], event["from"], event["to"], event["finished"]), ("s1", "working", "done", True))
        resp.close()
        conn.close()

        self.hub.observe([_session(status="working")])
        self.hub.observe([_session(status="blocked")])
        conn, resp = self._open({"Last-Event-ID": f"{self.hub.run}-1"})
        self._frame(resp)
        self.assertEqual(self._frame(resp)[0], f"id: {self.hub.run}-2")
        self.assertEqual(self._frame(resp)[0], f"id: {self.hub.run}-3")
        resp.close()
        conn.close()
        # A closed client is noticed on the next write (an event, or the 15 s ping) and forgotten.
        for i in range(50):
            if not self.hub._subscribers:
                break
            self.hub.observe([_session(status="working" if i % 2 else "done")])
            threading.Event().wait(0.02)
        self.assertEqual(self.hub._subscribers, [])


if __name__ == "__main__":
    unittest.main()
