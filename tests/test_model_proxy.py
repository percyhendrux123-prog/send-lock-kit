#!/usr/bin/env python3
"""model-proxy offline tests: loopback only, dummy keys only, no model API call."""
import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/one_door'))
import model_proxy as mp  # noqa: E402

DUMMY = 'SLKCANARY-' + '0123456789abcdef' * 2
POLICY = {'model_id': 'claude-test-1', 'max_tokens_cap': 2048, 'budget_usd': 1.0,
          'price_input_per_mtok': 2.0, 'price_output_per_mtok': 10.0, 'upstream_timeout_seconds': 2}


def body(**extra):
    b = {'model': 'anything', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'hi'}]}
    b.update(extra)
    return b


class Upstream(http.server.BaseHTTPRequestHandler):
    """Fake Anthropic endpoint. `mode` on the server picks the answer."""
    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        self.server.seen.append((dict(self.headers.items()), json.loads(raw)))
        mode = self.server.mode
        if mode == 'slow':
            time.sleep(4)
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', 'http://elsewhere.invalid/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if mode == 'stream':
            out, ctype = b'event: message_start\ndata: {}\n\n', 'text/event-stream'
        elif mode == 'server-tool':
            out, ctype = json.dumps({'type': 'message', 'model': 'm', 'content': [{'type': 'server_tool_use', 'id': 's', 'name': 'web_search', 'input': {}}]}).encode(), 'application/json'
        elif mode == 'status':
            self.send_response(529)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
            return
        else:
            out = json.dumps({'type': 'message', 'model': 'claude-test-1', 'role': 'assistant',
                              'content': [{'type': 'text', 'text': 'ok'}],
                              'usage': {'input_tokens': 1000, 'output_tokens': 100}}).encode()
            ctype = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def serve(self, server):
        t = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        t.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), t.join(2)))
        return server

    def stack(self, mode='ok', policy=None, allow_from=None):
        up = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        up.mode, up.seen = mode, []
        self.serve(up)
        self.up = up
        proxy = mp.make_server(('127.0.0.1', 0), policy=dict(POLICY, **(policy or {})), key=DUMMY,
                               upstream=mp.Upstream('127.0.0.1', up.server_port, plain=True),
                               log_path=self.dir / 'transcript.jsonl', state_dir=self.dir, allow_from=allow_from)
        self.proxy = self.serve(proxy)
        return proxy

    def call(self, payload, path='/v1/messages', headers=None, method='POST'):
        c = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
        data = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        h = {'Content-Type': 'application/json', 'Content-Length': str(len(data))}
        h.update(headers or {})
        c.request(method, path, body=data if method == 'POST' else None, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    def log(self):
        return [json.loads(x) for x in (self.dir / 'transcript.jsonl').read_text().splitlines()]


class BuildTests(unittest.TestCase):
    def test_fresh_request_from_allowlist_and_policy_model(self):
        out, notes = mp.build_upstream(body(max_tokens=99999, system='s', tools=[{'name': 't', 'input_schema': {}}],
                                            tool_choice={'type': 'auto'}), POLICY)
        self.assertEqual(out['model'], 'claude-test-1')
        self.assertEqual(out['max_tokens'], 2048)
        self.assertEqual(set(out), {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice'})
        self.assertTrue(any('capped' in n for n in notes))

    def test_unlisted_fields_are_refused_not_forwarded(self):
        for field in ('metadata', 'mcp_servers', 'container', 'context_management', 'service_tier', 'betas', 'extra_body'):
            with self.assertRaises(mp.Refused, msg=field):
                mp.build_upstream(body(**{field: {}}), POLICY)

    def test_server_side_tools_are_refused(self):
        for tool in ({'type': 'web_search_20250305', 'name': 'web_search'}, {'type': 'web_fetch_20250910', 'name': 'f'},
                     {'type': 'code_execution_20250825', 'name': 'c'}, {'type': 'bash_20250124', 'name': 'bash'},
                     {'name': 't', 'input_schema': {}, 'mcp_server': 'x'}):
            with self.assertRaises(mp.Refused, msg=str(tool)):
                mp.build_upstream(body(tools=[tool]), POLICY)
        mp.build_upstream(body(tools=[{'type': 'custom', 'name': 't', 'input_schema': {}}]), POLICY)

    def test_blocks_that_make_the_provider_fetch_are_refused(self):
        for block in ({'type': 'document', 'source': {'type': 'url', 'url': 'http://x'}},
                      {'type': 'image', 'source': {'type': 'url', 'url': 'http://x'}},
                      {'type': 'image', 'source': {'type': 'file', 'file_id': 'f'}},
                      {'type': 'server_tool_use', 'id': 'x', 'name': 'web_search', 'input': {}},
                      {'type': 'mcp_tool_use', 'id': 'x'}, {'type': 'container_upload', 'file_id': 'f'},
                      {'type': 'tool_result', 'tool_use_id': 'a', 'content': [{'type': 'document', 'source': {'type': 'url', 'url': 'u'}}]}):
            with self.assertRaises(mp.Refused, msg=str(block)):
                mp.build_upstream(body(messages=[{'role': 'user', 'content': [block]}]), POLICY)
        # Media is not needed by the test and is never forwarded, even inline.
        with self.assertRaises(mp.Refused):
            mp.build_upstream(body(messages=[{'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AA=='}}]}]), POLICY)

    def test_nested_shapes_are_rebuilt_and_unknown_nested_fields_refused(self):
        hermes_like = body(system=[{'type': 'text', 'text': 's', 'cache_control': {'type': 'ephemeral'}}],
                           tools=[{'name': 't', 'description': 'd', 'input_schema': {'type': 'object', 'properties': {'a': {'$ref': '#/defs/a'}}}}],
                           messages=[{'role': 'user', 'content': [{'type': 'text', 'text': 'hi', 'cache_control': {'type': 'ephemeral'}}]},
                                     {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'x', 'name': 't', 'input': {}}]},
                                     {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]}])
        out, _ = mp.build_upstream(hermes_like, POLICY)
        self.assertEqual(out['messages'][2]['content'][0], {'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'})
        bad = [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi', 'citations': [{'type': 'web_search_result_location', 'url': 'u'}]}]},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': [{'type': 'image', 'source': {}}]}]},
            {'role': 'user', 'content': 'hi', 'name': 'x'},
        ]
        for message in bad:
            with self.assertRaises(mp.Refused, msg=str(message)):
                mp.build_upstream(body(messages=[message]), POLICY)
        for tools in ([{'name': 't', 'input_schema': {'$ref': 'https://example.com/schema.json'}}],
                      [{'name': 't', 'input_schema': {}, 'eager_input_streaming': False}],
                      [{'name': 't', 'input_schema': {}, 'defer_loading': True}]):
            with self.assertRaises(mp.Refused, msg=str(tools)):
                mp.build_upstream(body(tools=tools), POLICY)

    def test_streaming_refused_and_hermes_fields_validated(self):
        with self.assertRaises(mp.Refused):
            mp.build_upstream(body(stream=True), POLICY)
        mp.build_upstream(body(stream=False, thinking={'type': 'adaptive', 'display': 'summarized'}, output_config={'effort': 'medium'}), POLICY)
        for bad in ({'thinking': {'type': 'enabled', 'budget_tokens': 8000}}, {'output_config': {'effort': 'low', 'format': {}}},
                    {'temperature': 3}, {'thinking': {'type': 'weird'}}):
            with self.assertRaises(mp.Refused, msg=str(bad)):
                mp.build_upstream(body(**bad), POLICY)

    def test_response_must_be_one_json_message_of_known_blocks(self):
        ok = json.dumps({'type': 'message', 'content': [{'type': 'tool_use', 'id': 'a', 'name': 'n', 'input': {}}]}).encode()
        mp.check_response(ok, 'application/json')
        for raw, ctype in ((ok, 'text/event-stream'), (b'{"type":"error"}', 'application/json'),
                           (json.dumps({'type': 'message', 'content': [{'type': 'web_search_tool_result'}]}).encode(), 'application/json')):
            with self.assertRaises(ValueError):
                mp.check_response(raw, ctype)


class KeyTests(Base):
    def write(self, value, mode=0o400):
        p = self.dir / 'key'
        if p.exists():
            p.chmod(0o600)
            p.unlink()
        p.write_text(value)
        p.chmod(mode)
        return p

    def test_key_file_must_be_read_only_regular_and_single_token(self):
        self.assertEqual(mp.read_key(self.write(DUMMY + '\n')), DUMMY)
        for value, mode in ((DUMMY, 0o600), (DUMMY, 0o644), ('two words', 0o400), ('', 0o400)):
            p = self.write(value, mode)
            with self.assertRaises(ValueError):
                mp.read_key(p)
        target = self.write(DUMMY)
        link = self.dir / 'link'
        link.symlink_to(target)
        with self.assertRaises(OSError):
            mp.read_key(link)

    def test_canary_key_never_goes_to_the_real_api_and_real_keys_never_to_rehearsal(self):
        with self.assertRaises(ValueError):
            mp.choose_upstream(DUMMY, None)
        with self.assertRaises(ValueError):
            mp.choose_upstream('sk-ant-not-a-canary', 'http://fake-anthropic:8080')
        for url in ('http://fake.example.com:8080', 'https://fake-anthropic:8080', 'http://127.0.0.1:8080'):
            with self.assertRaises(ValueError):
                mp.choose_upstream(DUMMY, url)
        up = mp.choose_upstream(DUMMY, 'http://fake-anthropic:8080')
        self.assertTrue(up.plain)
        prod = mp.choose_upstream('test-not-a-real-key', None)
        self.assertEqual((prod.host, prod.port, prod.plain), ('api.anthropic.com', 443, False))
        self.assertTrue(prod.context.check_hostname)
        self.assertEqual(prod.context.verify_mode, ssl.CERT_REQUIRED)

    def test_startup_errors_never_print_the_key(self):
        p = self.dir / 'policy.json'
        p.write_text(json.dumps(POLICY))
        key = self.write(DUMMY)
        r = subprocess.run([sys.executable, str(ROOT / 'deploy/one_door/model_proxy.py'), 'serve', '--policy', str(p),
                            '--key-file', str(key), '--state', str(self.dir), '--log', str(self.dir / 'l')],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 2)
        self.assertIn('canary key cannot be sent to the real API', r.stderr)
        self.assertNotIn(DUMMY, r.stdout + r.stderr)


class ServerTests(Base):
    def open_case(self, calls=5, seconds=60, case='c1'):
        mp.write_case(self.dir, case, calls, seconds)

    def test_no_case_window_refuses_every_call(self):
        self.stack()
        status, _ = self.call(body())
        self.assertEqual(status, 429)
        self.assertEqual(self.up.seen, [])

    def test_call_builds_fresh_headers_and_logs_full_bodies_without_the_key(self):
        self.stack()
        self.open_case()
        status, out = self.call(body(), headers={'Authorization': 'Bearer worker-token', 'anthropic-beta': 'x',
                                                 'X-Forwarded-For': '1.2.3.4', 'Cookie': 'c=1'})
        self.assertEqual(status, 200, out)
        headers, sent = self.up.seen[0]
        names = {k.lower() for k in headers}
        self.assertTrue(names <= {'host', 'accept-encoding', 'content-type', 'content-length', 'anthropic-version', 'x-api-key'}, names)
        self.assertEqual(headers.get('x-api-key'), DUMMY)
        self.assertEqual(sent['model'], 'claude-test-1')
        text = (self.dir / 'transcript.jsonl').read_text()
        self.assertNotIn(DUMMY, text)
        rows = self.log()
        call = [r for r in rows if r['event'] == 'call'][0]
        self.assertEqual(call['request']['messages'][0]['content'], 'hi')
        self.assertEqual(call['response']['content'][0]['text'], 'ok')
        self.assertEqual(call['model_returned'], 'claude-test-1')
        self.assertAlmostEqual(call['charged_usd'], (1000 * 2 + 100 * 10) / 1e6)
        self.assertGreater(call['reserve_usd'], (mp.OVERHEAD_TOKENS * 2 * 1.25 + 100 * 10) / 1e6)

    def test_only_post_v1_messages_and_allowed_source(self):
        self.stack(allow_from={'10.9.9.9'})
        self.open_case()
        self.assertEqual(self.call(body())[0], 403)
        self.assertEqual(self.call(body(), path='/v1/messages/count_tokens')[0], 404)
        self.assertEqual(self.call(body(), path='/v1/messages?beta=false')[0], 404)
        self.assertEqual(self.call(b'', method='GET')[0], 404)
        self.assertEqual(self.up.seen, [])

    def test_refusals_never_reach_upstream(self):
        self.stack()
        self.open_case(calls=20)
        for payload in (body(stream=True), body(metadata={}), body(tools=[{'type': 'web_search_20250305', 'name': 'w'}]), b'not json'):
            self.assertEqual(self.call(payload)[0], 400)
        self.assertEqual(self.up.seen, [])

    def test_per_case_call_and_time_caps(self):
        self.stack()
        self.open_case(calls=2)
        self.assertEqual(self.call(body())[0], 200)
        self.assertEqual(self.call(body())[0], 200)
        self.assertEqual(self.call(body())[0], 429)
        self.open_case(calls=5, seconds=0.2, case='c2')
        time.sleep(0.3)
        self.assertEqual(self.call(body())[0], 429)
        self.assertEqual(len(self.up.seen), 2)

    def test_spend_reservation_refuses_before_dispatch(self):
        # 1 MiB-ish request at $2/Mtok * 1.25 plus 2048 * $10/Mtok exceeds a $0.001 budget.
        self.stack(policy={'budget_usd': 0.001})
        self.open_case()
        status, _ = self.call(body(max_tokens=2048))
        self.assertEqual(status, 429)
        self.assertEqual(self.up.seen, [])
        self.assertTrue(any(r['event'] == 'cap' and 'spend cap' in r['reason'] for r in self.log()))

    def test_ambiguous_failures_keep_the_reservation_definite_errors_release_it(self):
        self.stack(mode='slow')
        self.open_case(calls=10)
        self.assertEqual(self.call(body())[0], 504)
        ledger = json.loads((self.dir / 'ledger.json').read_text())
        self.assertEqual(ledger['kept_ambiguous'], 1)
        self.assertGreater(ledger['spent_usd'], 0)
        spent = ledger['spent_usd']
        for mode, want in (('redirect', 502), ('status', 502), ('stream', 502), ('server-tool', 502)):
            self.up.mode = mode
            self.assertEqual(self.call(body())[0], want, mode)
        ledger = json.loads((self.dir / 'ledger.json').read_text())
        # A redirect is released; a 529 (server side) and unusable stream / server-tool answers are kept.
        self.assertEqual(ledger['kept_ambiguous'], 4)
        self.assertGreater(ledger['spent_usd'], spent)

    def test_incomplete_or_negative_usage_keeps_the_reservation(self):
        policy = mp.load_policy(json.dumps(POLICY))
        for usage in ({}, {'input_tokens': 5}, {'input_tokens': -1, 'output_tokens': 1}, {'input_tokens': 1.5, 'output_tokens': 1}, None):
            self.assertIsNone(mp.actual_usd(usage, policy), usage)
        self.assertGreater(mp.actual_usd({'input_tokens': 1, 'output_tokens': 1}, policy), 0)

    def test_policy_refuses_a_looser_reservation(self):
        for bad in ({'bytes_per_token_floor': 2}, {'cache_write_multiplier': 1.0}, {'budget_scope': 'a b'}):
            with self.assertRaises(ValueError, msg=str(bad)):
                mp.load_policy(json.dumps(dict(POLICY, **bad)))

    def test_pending_reservation_survives_a_crash_as_spend(self):
        state = self.dir
        ledger = mp.Ledger(state / 'ledger.json', 1.0)
        ledger.reserve('abc', 0.25, 'c1')
        with self.assertRaises(ValueError):
            mp.Ledger(state / 'ledger.json', 1.0)  # a second writer is refused while the first lives
        ledger._lock.close()                    # as when the process dies: the kernel drops the lock
        again = mp.Ledger(state / 'ledger.json', 1.0)  # as after a restart
        self.assertEqual(again.state['pending'], {})
        self.assertAlmostEqual(again.state['spent_usd'], 0.25)
        self.assertEqual(again.state['kept_ambiguous'], 1)
        with self.assertRaises(mp.Refused):
            again.reserve('def', 0.8, 'c1')
        again._lock.close()
        with self.assertRaises(ValueError):
            mp.Ledger(state / 'ledger.json', 1.0, 'another-scope')

    def test_an_overrun_halts_every_later_call(self):
        ledger = mp.Ledger(self.dir / 'ledger.json', 10.0)
        ledger.reserve('a', 0.01, 'c1')
        ledger.settle('a', 0.02)
        self.assertIn('exceeded its reservation', ledger.state['halted'])
        with self.assertRaises(mp.Refused):
            ledger.reserve('b', 0.01, 'c1')

    def test_one_call_at_a_time(self):
        self.stack(mode='slow', policy={'upstream_timeout_seconds': 6})
        self.open_case(calls=10)
        results = []
        first = threading.Thread(target=lambda: results.append(self.call(body())[0]))
        first.start()
        time.sleep(0.5)
        # The serial server does not even accept the second request until the first is answered.
        second = self.call(body())[0]
        first.join(10)
        self.assertEqual(results, [200])
        self.assertEqual(second, 200)
        self.assertEqual(len(self.up.seen), 2)


@unittest.skipUnless(shutil.which('openssl'), 'openssl CLI not available')
class TlsTests(Base):
    def tls_server(self, cert, keyfile):
        up = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        up.mode, up.seen = 'ok', []
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, keyfile)
        up.socket = ctx.wrap_socket(up.socket, server_side=True)
        return self.serve(up)

    def make_cert(self, name):
        cert, key = self.dir / (name + '.pem'), self.dir / (name + '.key')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=' + name,
                        '-addext', 'subjectAltName=DNS:' + name, '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True, timeout=60)
        return cert, key

    def test_default_trust_store_rejects_an_untrusted_certificate(self):
        cert, key = self.make_cert('localhost')
        up = self.tls_server(cert, key)
        prod = mp.production_upstream()
        target = mp.Upstream('localhost', up.server_port, prod.context)
        with self.assertRaises(ssl.SSLError):
            target.post(b'{}', {'content-type': 'application/json'}, 5)
        self.assertEqual(up.seen, [])

    def test_hostname_is_checked_even_with_a_trusted_certificate(self):
        cert, key = self.make_cert('other.invalid')
        up = self.tls_server(cert, key)
        ctx = ssl.create_default_context(cafile=str(cert))
        with self.assertRaises(ssl.SSLError):
            mp.Upstream('localhost', up.server_port, ctx).post(b'{}', {}, 5)
        good_cert, good_key = self.make_cert('localhost')
        good = self.tls_server(good_cert, good_key)
        status, _, _ = mp.Upstream('localhost', good.server_port, ssl.create_default_context(cafile=str(good_cert))).post(
            b'{"messages":[]}', {'content-type': 'application/json', 'content-length': '15'}, 5)
        self.assertEqual(status, 200)

    def test_ambient_proxy_variables_are_ignored(self):
        cert, key = self.make_cert('localhost')
        up = self.tls_server(cert, key)
        old = {k: os.environ.get(k) for k in ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY')}
        os.environ.update({'HTTPS_PROXY': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9', 'ALL_PROXY': 'http://127.0.0.1:9'})
        try:
            status, _, _ = mp.Upstream('localhost', up.server_port, ssl.create_default_context(cafile=str(cert))).post(
                b'{"messages":[]}', {'content-type': 'application/json', 'content-length': '15'}, 5)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual(status, 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
