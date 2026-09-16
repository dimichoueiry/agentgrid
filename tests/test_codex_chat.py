"""Codex CLI event/launch fixtures and engine routing regressions."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from agentgrid import chat, web


class CodexChatTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(chat, "codex_binary", return_value="codex")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nested_duplicate_errors(self):
        room = chat.ChatSession(None, "/repo", "codex")
        channel = room.subscribe()
        message = json.dumps({"error": {"message": "Model requires a newer version of Codex."}})
        self.run_fixture(room, [
            {"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}},
        ])
        self.assertEqual(channel.get_nowait()["message"], "Model requires a newer version of Codex.")
        self.assertTrue(channel.empty())

    def test_normalized_reply_and_lifecycle(self):
        self.assertEqual(chat.normalize_codex({'type': 'turn.started'}), [{'type': 'turn_started'}])
        self.assertEqual(chat.normalize_codex({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': 'hello'}}), [{'type': 'assistant_message', 'text': 'hello'}])
        self.assertTrue(chat.normalize_codex({'type': 'turn.completed', 'usage': {}})[0]['ok'])
        event = chat.normalize_codex({'type': 'turn.failed', 'error': {'message': 'quota'}})[0]
        self.assertEqual(event, {'type': 'error', 'message': 'quota'})

    def test_tool_start_finish_and_completion_only_change(self):
        item = {'id': 'c1', 'type': 'command_execution', 'command': 'ls'}
        event = chat.normalize_codex({'type': 'item.started', 'item': item})[0]
        self.assertEqual(event['input'], {'command': 'ls'})
        item.update(status='failed', exit_code=1, aggregated_output='permission denied')
        event = chat.normalize_codex({'type': 'item.completed', 'item': item})[0]
        self.assertFalse(event['ok'])
        self.assertEqual(event['summary'], 'permission denied')
        events = chat.normalize_codex({'type': 'item.completed', 'item': {
            'id': 'f1', 'type': 'file_change', 'status': 'completed'}})
        self.assertEqual([e['type'] for e in events], ['tool_use', 'tool_result'])
        self.assertEqual(chat.normalize_codex({'type': 'item.updated', 'item': item}), [])

    def run_fixture(self, room, events, message='--literal prompt', posture='read-only', model='codex-exact'):
        proc = mock.Mock(stdout=io.StringIO('\n'.join(json.dumps(e) for e in events)), returncode=0)
        with mock.patch.object(chat.subprocess, 'Popen', return_value=proc) as popen:
            room._run_turn(message, posture, model, ['/tmp/image with spaces.png'])
        return popen.call_args

    def test_resume_uses_codex_id_model_native_images_and_sandbox(self):
        room = chat.ChatSession('existing-id', '/repo', 'codex')
        channel = room.subscribe()
        call = self.run_fixture(room, [{'type': 'turn.started'}, {'type': 'turn.completed'}])
        argv = call.args[0]
        self.assertEqual(argv[:4], ['codex', 'exec', '-s', 'read-only'])
        self.assertIn('resume', argv)
        self.assertEqual(argv[-3:], ['--', 'existing-id', '--literal prompt'])
        self.assertEqual(argv[argv.index('--model') + 1], 'codex-exact')
        self.assertEqual(argv[argv.index('--image') + 1], '/tmp/image with spaces.png')
        self.assertEqual(call.kwargs['cwd'], '/repo')
        self.assertEqual(channel.get_nowait()['type'], 'turn_started')
        self.assertEqual(channel.get_nowait()['type'], 'turn_done')
        self.assertFalse(room.state()['running'])

    def test_new_session_adopts_id_and_missing_completion_reports_error(self):
        room = chat.ChatSession(None, '/repo', 'codex')
        channel = room.subscribe()
        call = self.run_fixture(room, [{'type': 'thread.started', 'thread_id': 'minted'}], posture='auto')
        self.assertEqual(room.session_id, 'minted')
        self.assertNotIn('resume', call.args[0])
        self.assertIn('workspace-write', call.args[0])
        self.assertEqual(channel.get_nowait()['type'], 'error')

    def test_launch_failure_names_codex(self):
        room = chat.ChatSession('id', '/repo', 'codex')
        channel = room.subscribe()
        with mock.patch.object(chat.subprocess, 'Popen', side_effect=FileNotFoundError('missing')):
            room._run_turn('hello', 'auto')
        self.assertIn('could not start codex', channel.get_nowait()['message'])

    def test_manager_retains_engine_when_stream_subscribes_before_send(self):
        manager = chat.ChatManager()
        room = manager.session('id', '/repo', 'codex')
        with mock.patch.object(room, 'send') as send:
            manager.send('id', '/repo', 'hello', 'auto', 'exact', engine='codex')
        self.assertEqual(room.engine, 'codex')
        send.assert_called_once_with('hello', 'auto', 'exact', None)

    def test_route_uses_tracked_engine_and_rejects_external_active_turn(self):
        handler = object.__new__(web.Handler)
        session = SimpleNamespace(session_id='id', cwd='/repo', engine='codex', status='idle')
        handler._session_by_id = mock.Mock(return_value=session)
        handler._valid_attachments = mock.Mock(return_value=[])
        handler._send_json = mock.Mock()
        handler.chat = mock.Mock()
        handler.chat.state.return_value = {'running': False}
        handler._chat_send({'sessionId': 'id', 'message': 'hi', 'engine': 'claude', 'model': 'exact'})
        self.assertEqual(handler.chat.send.call_args.kwargs, {'engine': 'codex'})
        session.status = 'working'
        handler.chat.send.reset_mock()
        handler._chat_send({'sessionId': 'id', 'message': 'hi'})
        self.assertEqual(handler._send_json.call_args.args[0], 409)
        handler.chat.send.assert_not_called()

    def test_a_thread_held_elsewhere_is_explained_not_echoed(self):
        # What codex-cli 0.153.4 prints when `exec resume` meets a thread an
        # interactive `codex` still holds: coloured tracing, then the verdict.
        lines = [
            '\x1b[2m2026-09-16T15:20:20.288753Z\x1b[0m \x1b[31mERROR\x1b[0m \x1b[2mcodex_core::session\x1b[0m: '
            'Failed to create session: thread-store conflict: thread id already has an active writer',
            'Error: thread/resume: thread/resume failed: thread id already has an active writer (code -32600)',
        ]
        room = chat.ChatSession('id', '/repo', 'codex')
        channel = room.subscribe()
        proc = mock.Mock(stdout=io.StringIO('\n'.join(lines)), returncode=1)
        with mock.patch.object(chat.subprocess, 'Popen', return_value=proc):
            room._run_turn('hello', 'auto')
        event = channel.get_nowait()
        self.assertEqual(event['type'], 'error')
        self.assertIn('open somewhere else', event['message'])
        self.assertIn('interactive terminal', event['message'])
        self.assertNotIn('\x1b', event['message'])
        self.assertTrue(channel.empty())

    def test_other_exit_noise_loses_its_colour_and_prefix(self):
        noise = '\x1b[31mERROR\x1b[0m something broke\nError: exec: model not found (code 1)\n\x1b[2mtrailing trace\x1b[0m'
        room = chat.ChatSession('id', '/repo', 'codex')
        channel = room.subscribe()
        proc = mock.Mock(stdout=io.StringIO(noise), returncode=2)
        with mock.patch.object(chat.subprocess, 'Popen', return_value=proc):
            room._run_turn('hello', 'auto')
        self.assertEqual(channel.get_nowait()['message'], 'exec: model not found (code 1)')
        self.assertEqual(chat.explain_exit('codex', '', 3), 'codex exited 3')
        self.assertEqual(chat.explain_exit('claude', '\x1b[1mError: boom\x1b[0m', 1), 'boom')

    def test_route_refuses_a_session_a_terminal_holds(self):
        handler = object.__new__(web.Handler)
        session = SimpleNamespace(session_id='id', cwd='/repo', engine='codex',
                                  status='done', pid=41614)
        handler._session_by_id = mock.Mock(return_value=session)
        handler._valid_attachments = mock.Mock(return_value=[])
        handler._send_json = mock.Mock()
        handler.chat = mock.Mock()
        handler.chat.state.return_value = {'running': False}
        handler._chat_send({'sessionId': 'id', 'message': 'hi'})
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, 409)
        self.assertIn('open in a terminal', body['error'])
        handler.chat.send.assert_not_called()
        # The same thread with its terminal closed is chattable again.
        session.pid = None
        handler._chat_send({'sessionId': 'id', 'message': 'hi'})
        handler.chat.send.assert_called_once()
        # And a claude session with a pid (its own terminal) was never in question.
        claude = SimpleNamespace(session_id='c', cwd='/repo', engine='claude', status='idle', pid=5)
        self.assertIsNone(web.codex_chat_block(claude, handler.chat))

    def test_a_turn_this_panel_started_is_queued_not_refused(self):
        session = SimpleNamespace(session_id='id', engine='codex', status='working', pid=41614)
        manager = mock.Mock()
        manager.state.return_value = {'running': True}
        self.assertIsNone(web.codex_chat_block(session, manager))

    def test_assigning_a_ticket_to_a_held_session_says_it_was_not_told(self):
        handler = object.__new__(web.Handler)
        session = SimpleNamespace(session_id='id', cwd='/repo', engine='codex',
                                  status='done', pid=41614, display_title='Codex')
        handler._session_by_id = mock.Mock(return_value=session)
        handler._ticket_id = mock.Mock(return_value='AG-9')
        handler._ticket_reply = mock.Mock()
        handler.chat = mock.Mock()
        handler.chat.state.return_value = {'running': False}
        with mock.patch.object(web.tickets, 'assign', return_value={'id': 'AG-9'}):
            handler._tickets_assign({'sessionId': 'id', 'ticketId': 'AG-9'})
        handler.chat.send.assert_not_called()
        _ticket, message = handler._ticket_reply.call_args.args
        self.assertIn('has not been told yet', message)
        self.assertIn('open in a terminal', message)
        self.assertFalse(handler._ticket_reply.call_args.kwargs['told'])

    def test_stop_cancels_process_group_and_clears_queue(self):
        room = chat.ChatSession('id', '/repo', 'codex')
        room._proc = mock.Mock(pid=42)
        room._proc.poll.return_value = None
        room._pending.put(('queued', 'auto', '', []))
        channel = room.subscribe()
        with mock.patch.object(chat.os, 'getpgid', return_value=42), mock.patch.object(chat.os, 'killpg') as kill:
            room.cancel()
        kill.assert_called_once()
        self.assertEqual(room.state()['queued'], 0)
        self.assertTrue(channel.get_nowait()['cancelled'])
