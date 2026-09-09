"""Regression fixtures for CLI and desktop Codex rollout formats."""
import json
import tempfile
import unittest
from pathlib import Path

from agentgrid import transcript


class CodexTranscriptTests(unittest.TestCase):
    def test_desktop_messages_tools_and_replies_are_visible(self):
        entries = [
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer',
                'content': [{'type': 'input_text', 'text': 'private harness instructions'}]}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': 'UserMessage', 'content': [{'type': 'text', 'text': 'Please fix it'}]}}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'Please fix it'}]}},
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': [],
                'encrypted_content': 'opaque-reasoning'}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': 'AgentMessage', 'phase': 'commentary',
                'content': [{'type': 'Text', 'text': 'Checking the files'}]}}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': 'CommandExecution', 'command': 'ls', 'status': 'completed',
                'aggregated_output': 'README.md', 'exit_code': 0}}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': 'AgentMessage', 'phase': 'final_answer',
                'content': [{'type': 'Text', 'text': 'Fixed'}]}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.codex' / 'sessions' / 'rollout.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text('\n'.join(json.dumps(e) for e in entries))
            blocks = transcript.render_blocks(path)
        self.assertEqual([b['kind'] for b in blocks], ['user', 'assistant', 'tool', 'result', 'assistant'])
        self.assertEqual(blocks[0]['text'], 'Please fix it')
        self.assertEqual(blocks[-1]['text'], 'Fixed')
        self.assertNotIn('private harness', str(blocks))
        self.assertNotIn('opaque-reasoning', str(blocks))

    def test_legacy_dialogue_and_custom_tools(self):
        def blocks(kind, payload):
            return transcript._codex_entry_blocks({'type': kind, 'payload': payload})
        self.assertEqual(blocks('event_msg', {'type': 'agent_message', 'phase': 'commentary',
            'message': 'Checking'})[0]['text'], 'Checking')
        self.assertEqual(blocks('response_item', {'type': 'custom_tool_call', 'name': 'apply_patch',
            'input': '*** Begin Patch'})[0]['kind'], 'tool')
        self.assertEqual(blocks('response_item', {'type': 'custom_tool_call_output',
            'output': 'Done'})[0]['text'], 'Done')
        self.assertEqual(blocks('response_item', {'type': 'reasoning', 'summary': [
            {'type': 'summary_text', 'text': 'Checking dependencies'}]}),
            [{'kind': 'thinking', 'text': 'Checking dependencies'}])

    def test_malformed_items_are_ignored(self):
        for item in (None, [], {}, {'type': 'AgentMessage', 'content': None}):
            self.assertEqual(transcript._codex_completed_item(item), [])
