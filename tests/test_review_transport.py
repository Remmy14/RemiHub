"""Credential-free real-CLI transport rehearsal; synthetic decisions are not real approval."""
import importlib.util
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from backend.core.codex_review_bounded import SourceReviewGateway
from backend.core.review_source import SourceSnapshot, SourceInspector

MAX_WIRE_BYTES=1024*1024
PASS={'decision':'approve','summary':'Synthetic transport result','findings':[]}


class SyntheticProvider:
    """Loopback-only, unauthenticated fixture endpoint. NEVER forwards requests.

    This is not an auth/token proxy or a replacement model backend. The exact
    request tool list is checked before returning a synthetic response.
    """
    def __init__(self, *, attack=None, verdict=PASS, hold=False, fail_second=False):
        self.attack = attack
        self.verdict = verdict
        self.hold = hold
        self.requests = []
        self.failures = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_second = fail_second
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                try:
                    if self.headers.get('Authorization'):
                        raise ValueError('Credentials reached credential-free qualification')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length < 4 * MAX_WIRE_BYTES:
                        raise ValueError('Invalid synthetic request size')
                    request = json.loads(self.rfile.read(length))
                    owner.requests.append(request)
                    if owner.fail_second and len(owner.requests) == 2:
                        body=json.dumps({'error':{'message':'Fixture upstream failed before inference','type':'server_error'}}).encode()
                        self.send_response(400);self.send_header('Content-Type','application/json');self.end_headers()
                        self.wfile.write(body);return
                    tools = request.get('tools', [])
                    names = [t.get('name') for t in tools]
                    if names != []:
                        raise ValueError('Unexpected tool registry: ' + json.dumps(tools)[:2000])
                    owner.entered.set()
                    if owner.hold:
                        if not owner.release.wait(20):
                            raise TimeoutError('Synthetic held turn expired')
                    outputs = [{'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                                'status': 'completed', 'phase': 'final_answer',
                                'content': [{'type': 'output_text', 'text': json.dumps({'response': owner.verdict})}]}]
                    if owner.attack and len(owner.requests) == 1:
                        outputs = owner.attack
                    response = {'id': 'resp_' + str(len(owner.requests)), 'object': 'response',
                                'status': 'completed', 'output': outputs,
                                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                    events = [{'type': 'response.created', 'response': dict(response, output=[], status='in_progress')}]
                    for i, item in enumerate(outputs):
                        events += [{'type': 'response.output_item.added', 'output_index': i, 'item': item},
                                   {'type': 'response.output_item.done', 'output_index': i, 'item': item}]
                    events.append({'type': 'response.completed', 'response': response})
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    for event in events:
                        self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    owner.failures.append(str(exc))
                    print("Synthetic rejection:", str(exc), flush=True)
                    self.send_error(400, 'Synthetic qualification failed')
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def config(self):
        return {'name': 'synthetic', 'base_url': f'http://127.0.0.1:{self.server.server_port}/v1',
                'wire_api': 'responses', 'requires_openai_auth': False,
                'request_max_retries': 0, 'stream_max_retries': 0}

    def __exit__(self, *args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def verify(self):
        if self.failures or not self.requests:
            raise RuntimeError('Transport qualification failed: ' + repr(self.failures))
        if any([t.get('name') for t in r.get('tools', [])] != [] for r in self.requests):
            raise RuntimeError('Unexpected tool catalog')

@unittest.skipUnless(os.environ.get('REMIHUB_TEST_CODEX_BIN') and importlib.util.find_spec('openai_codex'),
                     'Run separately with the existing Codex SDK environment and explicit test CLI')
class SourceTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='review-transport-fixture-');self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.home=root/'home';self.home.mkdir(mode=0o700)
        self.source=root/'source';self.source.mkdir();(self.source/'Api.kt').write_text('val contract = 42\n')
        (self.home/'auth-sentinel').write_text('NEVER_EXPOSE_CONTROL_SENTINEL')
        self.inspector=SourceInspector({'candidate':SourceSnapshot(self.source)},b'diff fixture')
        binary=os.environ['REMIHUB_TEST_CODEX_BIN']
        self.gateway=SourceReviewGateway(codex_bin=binary,control_home=self.home,timeout_seconds=15)

    def run_provider(self, provider):
        self.gateway.prepare()
        return self.gateway.run_turn(repository_path=self.source,prompt='Inspect Api.kt and report the fixture decision.',
            model='gpt-6-sol',inspector=self.inspector,_provider=provider.config)

    def test_source_tool_round_trip_uses_frozen_source_without_exposing_control_home(self):
        call=[{'id':'msg_read','type':'message','role':'assistant','status':'completed',
               'content':[{'type':'output_text','text':json.dumps({'response':{'source_request':{'operation':'read','repository':'candidate','path':'Api.kt','offset':0,'limit':100,'query':''}}})}]}]
        with SyntheticProvider(attack=call) as provider:
            result=self.run_provider(provider);provider.verify()
            self.assertEqual(json.loads(result['response']),PASS)
            self.assertGreaterEqual(len(provider.requests),2)
            for request in provider.requests:
                format = request['text']['format']
                self.assertEqual(format['type'], 'json_schema')
                self.assertTrue(format['strict'])
                self.assertEqual(format['schema']['required'], ['response'])
            self.assertEqual(len(result['turns']),3)
            self.assertTrue(all(t['usage']['last']['total_tokens'] > 0 for t in result['turns']))
            serialized=json.dumps(provider.requests)
            self.assertIn('val contract = 42',serialized)
            self.assertNotIn('NEVER_EXPOSE_CONTROL_SENTINEL',serialized)
            self.assertEqual(self.inspector.calls[0]['path'],'Api.kt')

    def test_immediate_approval_waits_until_every_verified_diff_chunk_is_delivered(self):
        from backend.core.review_source import MAX_RESULT
        diff = b'd' * (MAX_RESULT * 2 + 1)
        self.inspector = SourceInspector({'candidate': SourceSnapshot(self.source)}, diff)
        # Even a provider that immediately approves must receive all source evidence.
        with SyntheticProvider() as provider:
            result = self.run_provider(provider);provider.verify()
            self.assertEqual(len(result['turns']), 4)
            self.assertEqual([call['offset'] for call in self.inspector.calls], [0, MAX_RESULT, MAX_RESULT * 2])
            self.assertIsNone(self.inspector.next_diff_chunk())
            self.assertEqual(json.loads(result['response']), PASS)
            serialized = json.dumps(provider.requests)
            self.assertIn('complete diff must reach you', serialized)

    def test_malformed_output_is_rejected_without_relaxing_decision_validation(self):
        responses = [json.dumps({'response': PASS}) + '\nstray text',
                     json.dumps({'response': dict(PASS, findings=[{'issue':'bad','required_change':'fix'}])}),
                     json.dumps({'response': PASS, 'unexpected': True})]
        for raw in responses:
            attack=[{'id':'invalid','type':'message','role':'assistant','status':'completed',
                     'content':[{'type':'output_text','text':raw}]}]
            with self.subTest(raw=raw), SyntheticProvider(attack=attack) as provider:
                with self.assertRaises(ValueError) as caught:self.run_provider(provider)
                provider.verify()
                self.assertIsNotNone(getattr(caught.exception, 'codex_usage', None))
                self.assertGreater(caught.exception.codex_usage['last']['total_tokens'], 0)

    def test_upstream_failure_does_not_reuse_previous_turn_usage(self):
        call=[{'id':'read','type':'message','role':'assistant','status':'completed',
               'content':[{'type':'output_text','text':json.dumps({'response':{'source_request':{
                   'operation':'read','repository':'candidate','path':'Api.kt','offset':0,'limit':100,'query':''}}})}]}]
        with SyntheticProvider(attack=call, fail_second=True) as provider:
            with self.assertRaises(RuntimeError) as caught:self.run_provider(provider)
            provider.verify()
            self.assertEqual(len(caught.exception.codex_turns),1)
            self.assertGreater(caught.exception.codex_turns[0]['usage']['last']['total_tokens'],0)
            self.assertIsNone(caught.exception.codex_usage)
            self.assertIn('retry frozen candidate', str(caught.exception))

    def test_injected_shell_is_not_executed(self):
        marker=self.source/'unexpected-write'
        attack=[{'type':'function_call','id':'fc_shell','call_id':'call_shell','name':'exec_command',
                 'arguments':json.dumps({'cmd':'touch '+str(marker)})}]
        with SyntheticProvider(attack=attack) as provider:
            # The server rejects an unknown tool before host request dispatch.
            try:self.run_provider(provider)
            except Exception:pass
            provider.verify()
            self.assertFalse(marker.exists())
            # A rejected native call may be followed by the host's required diff delivery.
            self.assertTrue(all(call['operation'] == 'diff' for call in self.inspector.calls))

    def test_native_shell_and_patch_variants_cannot_execute(self):
        marker=self.source/'unexpected-write'
        variants=[
            [{'type':'local_shell_call','id':'shell_1','call_id':'shell_call','status':'completed',
              'action':{'type':'exec','command':['/bin/sh','-c','touch '+str(marker)],'timeout_ms':1000}}],
            [{'type':'custom_tool_call','id':'patch_1','call_id':'patch_call','name':'apply_patch',
              'input':'*** Begin Patch\n*** Add File: '+str(marker)+'\n+bad\n*** End Patch'}]]
        for attack in variants:
            with self.subTest(attack=attack[0]['type']),SyntheticProvider(attack=attack) as provider:
                try:self.run_provider(provider)
                except Exception:pass
                provider.verify();self.assertFalse(marker.exists())

    def test_interruption_closes_private_process(self):
        with SyntheticProvider(hold=True) as provider:
            errors=[]
            def run():
                try:self.run_provider(provider)
                except Exception as exc:errors.append(type(exc).__name__)
            thread=threading.Thread(target=run);thread.start()
            self.assertTrue(provider.entered.wait(10));self.gateway.cancel();provider.release.set();thread.join(5)
            self.assertFalse(thread.is_alive());self.assertTrue(errors)

if __name__=='__main__':unittest.main()
