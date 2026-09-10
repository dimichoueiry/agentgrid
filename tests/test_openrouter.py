import io
import json
import os
import unittest
from unittest import mock

from agentgrid import chat, credentials, openrouter, teams, web
from test_teams import _run


def stream(*chunks):
    return io.BytesIO((''.join('data: ' + (c if isinstance(c, str) else json.dumps(c)) + '\n\n' for c in chunks)).encode())


class OpenRouterTests(unittest.TestCase):
    def test_streamed_text_usage_and_history(self):
        room = chat.ChatSession(None, '/tmp', 'openrouter'); events = room.subscribe()
        response = stream({'choices': [{'delta': {'content': 'Hello '}}]},
            {'choices': [{'delta': {'content': 'world'}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'cost': .002}}, '[DONE]')
        with mock.patch.object(credentials, 'get_key', return_value='secret'), \
                mock.patch.object(openrouter, 'request', return_value=response) as request:
            room._run_turn('write', 'read-only', 'provider/model')
        messages=[]
        while not events.empty(): messages.append(events.get_nowait())
        self.assertEqual([e['type'] for e in messages], ['turn_started','assistant_delta','assistant_delta','assistant_message','turn_done'])
        self.assertEqual(messages[-1]['result'], 'Hello world')
        self.assertEqual(messages[-1]['stats']['costUsd'], .002)
        self.assertEqual(room._api_history[-1]['content'], 'Hello world')
        self.assertNotIn('secret', str(messages))
        self.assertNotIn('tools', request.call_args.args[2])
        self.assertFalse(room.state()['running'])

    def test_stream_errors_and_truncation_never_report_success(self):
        for response in (stream({'error': {'message': 'reflected secret'}}),
                         stream({'choices': [{'delta': {'content': 'partial'}, 'finish_reason': 'length'}]}, '[DONE]'),
                         stream({'choices': [{'delta': {'content': 'partial'}}]})):
            room=chat.ChatSession(None, '/tmp', 'openrouter'); events=room.subscribe()
            with mock.patch.object(credentials,'get_key',return_value='secret'),mock.patch.object(openrouter,'request',return_value=response):
                room._run_turn('hello','auto','provider/model')
            output=[]
            while not events.empty(): output.append(events.get_nowait())
            self.assertEqual(output[-1]['type'],'error')
            self.assertNotIn('secret',str(output))
            self.assertEqual(room._api_history,[])

    def test_sse_comments_multiline_and_done(self):
        raw=io.BytesIO(b': ping\n\ndata: {"a":\ndata: 1}\n\ndata: [DONE]\n\n')
        self.assertEqual(list(openrouter.sse_events(raw)), ['{"a":\n1}', '[DONE]'])

    def test_missing_model_does_not_make_request(self):
        room=chat.ChatSession(None,'/tmp','openrouter'); events=room.subscribe()
        with mock.patch.object(openrouter,'request') as request:
            room._run_turn('hi','auto','')
        request.assert_not_called(); self.assertEqual(events.get_nowait()['type'],'error')

    def test_cancelled_turn_never_opens_connection(self):
        room=chat.ChatSession(None,'/tmp','openrouter')
        room.cancel()
        with mock.patch.object(openrouter,'request') as request:
            room._run_turn('hi','auto','provider/model')
        request.assert_not_called()

    def test_key_is_not_in_subprocess_environment(self):
        room=chat.ChatSession('id','/tmp','claude')
        proc=mock.Mock(stdout=io.StringIO('{"type":"result","is_error":false}\n'),returncode=0)
        with mock.patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret'}),mock.patch.object(chat.subprocess,'Popen',return_value=proc) as popen:
            room._run_turn('hi','auto')
        self.assertNotIn('OPENROUTER_API_KEY',popen.call_args.kwargs['env'])

    def test_public_status_does_not_expose_key(self):
        with mock.patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret'}):
            status=credentials.connection_status()
        self.assertTrue(status['connected']); self.assertNotIn('secret',str(status))

    def test_catalog_filters_nontext_and_caches(self):
        openrouter.clear_cache()
        response=io.BytesIO(json.dumps({'data':[
            {'id':'a/model','name':'Model','supported_parameters':['tools']},
            {'id':'a/image','architecture':{'output_modalities':['image']}}]}).encode())
        with mock.patch.object(credentials,'get_key',return_value='secret'),mock.patch.object(openrouter,'request',return_value=response) as request:
            first=openrouter.model_catalog(); second=openrouter.model_catalog()
        self.assertEqual(first,second); self.assertEqual(len(first),1); self.assertTrue(first[0]['tools']);request.assert_called_once()
        openrouter.clear_cache()


class CoordinatorTests(unittest.TestCase):
    def team(self,cap=2):
        return teams.load_team({'name':'Coordinator test','cwd':'/tmp','nodes':[
            {'id':'writer','role':'Writer','prompt':'Write','engine':'openrouter','model':'provider/model','includeContext':True}],
            'coordinator':{'engine':'claude','model':'sonnet','maxDelegations':cap}})

    def test_delegates_then_finishes_using_result(self):
        seen=[]
        def run_node(run,node,prompt):
            seen.append((node.id,prompt))
            if node.id=='writer':return 'Complete draft',False
            if len(seen)==1:return '{"action":"delegate","agent":"writer","task":"Write lesson"}',False
            self.assertIn('Complete draft',prompt)
            return '{"action":"finish","result":"Reviewed final"}',False
        with mock.patch.object(teams.TeamRun,'_run_node',run_node):
            run,events=_run(self.team(),'brief')
        self.assertTrue(run.ok);self.assertEqual(run.final_result,'Reviewed final')
        self.assertEqual([x[0] for x in seen],['__coordinator','writer','__coordinator'])
        self.assertEqual(sum(e['type']=='delegation' for e in events),1)

    def test_unknown_agent_and_invalid_json_fail_closed(self):
        for result in ('not JSON','{"action":"delegate","agent":"unapproved","task":"Do stuff"}'):
            with mock.patch.object(teams.TeamRun,'_run_node',return_value=(result,False)):
                run,_=_run(self.team(),'brief')
            self.assertFalse(run.ok); self.assertTrue(run.reason)

    def test_cap_stops_repeated_delegation(self):
        def node_result(run,node,prompt):
            return ('{"action":"delegate","agent":"writer","task":"Revise"}' if node.id=='__coordinator' else 'draft'),False
        with mock.patch.object(teams.TeamRun,'_run_node',node_result):run,events=_run(self.team(1),'brief')
        self.assertFalse(run.ok);self.assertIn('limit',run.reason)
        self.assertEqual(sum(e['type']=='delegation' for e in events),1)

    def test_coordinator_roundtrip_and_explicit_openrouter_model(self):
        team=self.team()
        self.assertEqual(teams.load_team(teams.team_to_dict(team)),team)
        raw=teams.team_to_dict(team);raw['coordinator']['engine']='openrouter';raw['coordinator']['model']=''
        with self.assertRaisesRegex(ValueError,'model'):teams.load_team(raw)
