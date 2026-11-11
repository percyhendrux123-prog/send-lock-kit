#!/usr/bin/env python3
"""Local fake upstreams only; exercise method/path/origin wall and no redirects."""
import http.server
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy/one_door'))
try:
    import gateway
except ImportError:
    gateway = None

class GatewayTests(unittest.TestCase):
    def test_fixed_route_policy(self):
        self.assertIsNotNone(gateway, 'allowlist gateway missing')
        rules = {'google': {'origin':'http://fake-google:8080', 'methods':['GET'], 'prefixes':['/read/']},
                 'model': {'origin':'https://fake-model.test', 'methods':['POST'], 'prefixes':['/v1/']}}
        self.assertEqual(gateway.target(rules,'GET','/google/read/mail'), 'http://fake-google:8080/read/mail')
        for method, path in [('POST','/google/read/mail'),('CONNECT','fake-google:443'),
                             ('GET','http://outside.test/'),('GET','/google/send'),
                             ('GET','/google/read/../send'),('GET','/google/read/%2e%2e/send'),
                             ('GET','/google/read//send'),('GET','/google/read/hi\\send'),
                             ('POST','/missing/v1/chat')]:
            self.assertIsNone(gateway.target(rules,method,path), (method,path))
        self.assertIsNone(gateway.target({},'GET','/google/read/mail'))

    def test_denial_helpers_do_not_accept_http_failures_as_network_proof(self):
        import container_probe
        def http_failure():
            raise urllib.error.HTTPError('http://fake',500,'fake error',{},None)
        with self.assertRaises(AssertionError): container_probe.denied('not network denial',http_failure)
        def unavailable(): raise urllib.error.URLError('fake unreachable')
        container_probe.denied('synthetic transport failure',unavailable)

    def test_negative_network_probe_fails_when_silent_socket_is_reachable(self):
        import container_probe
        import socketserver
        class Silent(socketserver.BaseRequestHandler):
            def handle(self): self.request.recv(100)
        server = socketserver.TCPServer(('127.0.0.1',0),Silent)
        t = threading.Thread(target=server.serve_forever,daemon=True); t.start()
        self.addCleanup(lambda: (server.shutdown(),server.server_close(),t.join()))
        for method in ['GET','POST']:
            with self.assertRaises(AssertionError):
                container_probe.http_network_denied('silent but reachable',f'http://127.0.0.1:{server.server_address[1]}/',method,b'fake' if method=='POST' else None)

    def test_actual_loopback_gateway_strips_redirects(self):
        self.assertIsNotNone(gateway, 'allowlist gateway missing')
        seen = []
        class Fake(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302 if self.path.endswith('/redirect') else 200)
                self.send_header('Location','http://127.0.0.1:1/never')
                self.end_headers()
                self.wfile.write(b'fake data')
            def log_message(self, *args): pass
        upstream = http.server.ThreadingHTTPServer(('127.0.0.1',0),Fake)
        wall = gateway.server(('127.0.0.1',0), {'google':{
            'origin':f'http://127.0.0.1:{upstream.server_port}', 'methods':['GET'], 'prefixes':['/read/']}})
        for srv in [upstream, wall]:
            t = threading.Thread(target=srv.serve_forever,daemon=True); t.start()
            self.addCleanup(lambda s=srv,t=t: (s.shutdown(),s.server_close(),t.join()))
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f'http://127.0.0.1:{wall.server_port}'
        with op.open(base+'/google/read/mail') as response:
            self.assertEqual(response.read(), b'fake data')
        for path, data, expected in [('/google/read/mail', b'send',403),('/google/send',None,403),
                                     ('/google/read/redirect',None,502)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                op.open(base+path,data=data)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
        import container_probe
        for method in ['GET','POST','CONNECT']:
            container_probe.proxy_denied(method, 'outside.test:443' if method == 'CONNECT' else 'http://outside.test/',host='127.0.0.1',port=wall.server_port)
        self.assertEqual(seen, ['/read/mail','/read/redirect'])

if __name__ == '__main__': unittest.main(verbosity=2)
