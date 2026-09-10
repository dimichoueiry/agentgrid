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
