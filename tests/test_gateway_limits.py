#!/usr/bin/env python3
"""Loopback-only resource limits; no change to route permission decisions."""
import http.server
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy/one_door'))
import gateway


class GatewayLimitTests(unittest.TestCase):
    def start(self, *, routes=None, **limits):
        seen = []
        class Fake(http.server.BaseHTTPRequestHandler):
            def reply(self):
                seen.append((self.command, self.path, self.rfile.read(int(self.headers.get('Content-Length', 0)))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'fake data')
            do_GET = do_POST = reply
            def log_message(self, *args): pass
        upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        self.addCleanup(upstream.server_close)
        rules = {name: {'origin':f'http://127.0.0.1:{upstream.server_port}', 'methods':[method], 'prefixes':[prefix]}
                 for name, method, prefix in [('google', 'GET', '/read/'), ('model', 'POST', '/v1/')]}
        for name, settings in (routes or {}).items():
            rules.setdefault(name, {**rules['model'], 'prefixes': ['/echo/']}).update(settings)
        wall = gateway.server(('127.0.0.1', 0), rules, **limits)
        for server in (upstream, wall):
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01}, daemon=True)
            thread.start()
            self.addCleanup(lambda s=server, t=thread: (s.shutdown(), s.server_close(), t.join(2)))
        return wall, seen

    def connect(self, wall):
        client = socket.create_connection(('127.0.0.1', wall.server_port), timeout=2)
        self.addCleanup(client.close)
        return client

    def test_stalled_and_slow_drip_clients_have_an_absolute_read_deadline(self):
        wall, seen = self.start(read_timeout=0.25, max_clients=2)
        for kind in ('idle', 'headers', 'body', 'drip-headers', 'drip-body'):
            with self.subTest(kind=kind):
                client = self.connect(wall)
                started = time.monotonic()
                if kind in ('body', 'drip-body'):
                    client.sendall(b'POST /model/v1/chat HTTP/1.1\r\nContent-Length: 100\r\n\r\n')
                elif kind != 'idle':
                    client.sendall(b'GET /google/read/mail HTTP/1.1\r\nX-Fake: ')
                stop = threading.Event()
                def drip():
                    while not stop.wait(0.04):
                        try:
                            client.sendall(b'x')
                        except OSError:
                            return
                thread = threading.Thread(target=drip, daemon=True)
                if kind.startswith('drip'):
                    thread.start()
                try:
                    while True:
                        try:
                            raw = client.recv(4096)
                        except ConnectionResetError:
                            break
                        if not raw:
                            break
                finally:
                    stop.set()
                    if thread.is_alive():
                        thread.join(2)
                    client.close()
                self.assertLess(time.monotonic()-started, 1.5, 'deadline reset by incoming bytes')
                self.assertEqual(seen, [], 'an incomplete request reached upstream')
        self.assertEqual(self.get(wall, '/google/read/mail'), b'fake data')
        self.assertEqual(seen, [('GET', '/read/mail', b'')])

    def test_complete_large_body_and_route_decisions_are_unchanged(self):
        wall, seen = self.start(read_timeout=1, max_clients=2)
        raw = b'{"fake":"'+b'x'*16000+b'"}'
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f'http://127.0.0.1:{wall.server_port}/model/v1/chat',
                                         data=raw, headers={'Content-Type':'application/json'})
        with op.open(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'fake data')
        self.assertEqual(seen, [('POST', '/v1/chat', raw)])
        with self.assertRaises(urllib.error.HTTPError) as refused:
            op.open(f'http://127.0.0.1:{wall.server_port}/google/read/mail', data=b'fake', timeout=2)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()
        self.assertEqual(len(seen), 1)

    def test_invalid_limits_refuse_before_binding(self):
        for limits in ({'read_timeout':0}, {'read_timeout':-1}, {'read_timeout':float('inf')},
                       {'read_timeout':float('nan')}, {'max_clients':0}, {'max_clients':True}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                gateway.server(('127.0.0.1', 0), {}, **limits)

    def test_configured_request_cap_applies_only_to_matching_route(self):
        wall, seen = self.start(routes={'echo': {'request_max_bytes': 1024 * 1024}})
        base = f'http://127.0.0.1:{wall.server_port}'
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path, size, expected in [('/echo/echo/data', 1024 * 1024, 200),
                                     ('/echo/echo/data', 1024 * 1024 + 1, 502),
                                     ('/model/v1/chat', gateway.LIMIT, 200),
                                     ('/model/v1/chat', gateway.LIMIT + 1, 502),
                                     ('/echo/other/data', 1, 403)]:
            with self.subTest(path=path, size=size):
                if expected == 200:
                    with op.open(base+path, data=b'x'*size, timeout=3) as reply:
                        self.assertEqual(reply.code, 200)
                else:
                    with self.connect(wall) as client:
                        client.sendall(f'POST {path} HTTP/1.1\r\nContent-Length: {size}\r\n\r\n'.encode())
                        reply = http.client.HTTPResponse(client)
                        with reply:
                            reply.begin()
                            self.assertEqual(reply.status, expected)
        self.assertEqual([(method, path, len(raw)) for method, path, raw in seen],
                         [('POST', '/echo/data', 1024 * 1024), ('POST', '/v1/chat', gateway.LIMIT)])

    def test_upstream_socket_timeout_is_selected_by_route(self):
        wall, seen = self.start(routes={'echo': {'upstream_timeout_seconds': 180}})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        observed = []
        real_open = urllib.request.OpenerDirector.open
        def record(opener, fullurl, data=None, timeout=0):
            if isinstance(fullurl, urllib.request.Request) and fullurl.selector in {'/echo/data', '/v1/chat'}:
                observed.append((fullurl.selector, timeout, dict(fullurl.header_items())))
            return real_open(opener, fullurl, data, timeout)
        with patch.object(urllib.request.OpenerDirector, 'open', record):
            for route in ('/echo/echo/data', '/model/v1/chat'):
                url = f'http://127.0.0.1:{wall.server_port}'+route
                req = urllib.request.Request(url, data=b'{}', headers={'X-Dummy-Header': 'not forwarded'})
                with op.open(req, timeout=3) as response:
                    self.assertEqual(response.code, 200)
        self.assertEqual(observed, [('/echo/data', 180, {'Content-type': 'application/json'}),
                                    ('/v1/chat', 4, {'Content-type': 'application/json'})])

    def test_malformed_route_limit_is_rejected_before_binding(self):
        for settings in ({'request_max_bytes': True}, {'request_max_bytes': -1},
                         {'request_max_bytes': 1024 * 1024 + 1}, {'request_max_bytes': '12'},
                         {'upstream_timeout_seconds': True}, {'upstream_timeout_seconds': -1},
                         {'upstream_timeout_seconds': float('nan')}, {'upstream_timeout_seconds': 181},
                         {'upstream_timeout_seconds': '4'}, {'upstream_timeout_seconds': 0}):
            with self.subTest(settings=settings), patch.object(gateway.http.server.ThreadingHTTPServer,
                                                              'server_bind', side_effect=AssertionError) as bind:
                with self.assertRaises(ValueError):
                    gateway.server(('127.0.0.1', 0), {'echo': settings})
                bind.assert_not_called()

    def get(self, wall, path):
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(f'http://127.0.0.1:{wall.server_port}'+path, timeout=2) as response:
            return response.read()

    def test_concurrency_is_bounded_and_capacity_recovers(self):
        wall, seen = self.start(read_timeout=2, max_clients=2)
        condition = threading.Condition()
        counts = {'active':0, 'peak':0}
        original = wall.RequestHandlerClass
        class Observed(original):
            def setup(self):
                super().setup()
                with condition:
                    counts['active'] += 1
                    counts['peak'] = max(counts['peak'], counts['active'])
                    condition.notify_all()
            def finish(self):
                try:
                    super().finish()
                finally:
                    with condition:
                        counts['active'] -= 1
                        condition.notify_all()
        wall.RequestHandlerClass = Observed
        clients = [self.connect(wall) for _ in range(2)]
        with condition:
            self.assertTrue(condition.wait_for(lambda: counts['active'] == 2, timeout=1))
        extra = self.connect(wall)
        try:
            extra.sendall(b'GET /google/read/mail HTTP/1.1\r\n\r\n')
            self.assertEqual(extra.recv(4096), b'')
        except (ConnectionResetError, BrokenPipeError):
            pass  # Over-capacity sockets close without creating a handler.
        self.assertEqual(seen, [])
        for client in clients:
            client.shutdown(socket.SHUT_RDWR)
            client.close()
        with condition:
            self.assertTrue(condition.wait_for(lambda: counts['active'] == 0, timeout=1))
        # Handler finish precedes the slot release by a few instructions.
        deadline = time.monotonic()+1
        while True:
            try:
                self.assertEqual(self.get(wall, '/google/read/mail'), b'fake data')
                break
            except (OSError, http.client.HTTPException):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.get(wall, '/google/send')
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()
        self.assertEqual(counts['peak'], 2)
        self.assertEqual(seen, [('GET', '/read/mail', b'')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
