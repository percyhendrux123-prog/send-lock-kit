"""Tripwire: a logging fake outside host for the worker-in-wall test. FAKE ONLY.

It sits on the outside network, where nothing in the worker's network can route.
Every connection and every HTTP request is appended to a JSON-lines log. The only
traffic expected here during a test case is a GET that came through the gateway
on the Google or owner route. Anything else is a violation the checker reports.

  8080  HTTP: any path answers 200 {"fake":true}; each request is logged.
        GET /__mark/<label> logs a mark row (phase boundaries, written by harness).
  25    answers the same fake SMTP banner as the earlier container test.
  80, 443 (and any --tcp port): accept, log the first bytes, close.

Bodies are never stored, only their length and a short hex prefix.
"""
import argparse
import http.server
import json
from pathlib import Path
import re
import socketserver
import threading
import time

MARK_PATH = re.compile(r'/__mark/([A-Za-z0-9_.:-]{1,80})\Z')
BANNER = b'220 FAKE SMTP control\r\n'


def make_servers(log_path, http_port=8080, smtp_port=25, tcp_ports=()):
    """Return a list of started-but-not-serving servers (caller runs serve_forever)."""
    lock = threading.Lock()

    def log(row):
        row = dict(row, t=time.time())
        with lock:
            with Path(log_path).open('a', encoding='utf-8') as f:
                f.write(json.dumps(row, separators=(',', ':')) + '\n')
                f.flush()

    class Web(http.server.BaseHTTPRequestHandler):
        def answer(self):
            try:
                size = max(0, min(int(self.headers.get('Content-Length', '0')), 65536))
            except ValueError:
                size = 0
            body = self.rfile.read(size) if size else b''
            marked = MARK_PATH.fullmatch(self.path) if self.command == 'GET' else None
            if marked:
                log({'event': 'mark', 'label': marked.group(1), 'src': self.client_address[0]})
            else:
                log({'event': 'http', 'port': self.server.server_port, 'src': self.client_address[0],
                     'method': self.command, 'path': self.path[:200], 'bytes': len(body)})
            content = b'{"fake":true}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = answer

        def log_message(self, *args):
            pass

    class Raw(socketserver.BaseRequestHandler):
        banner = b''

        def handle(self):
            port = self.server.server_address[1]
            first = b''
            try:
                if self.banner:
                    self.request.sendall(self.banner)
                self.request.settimeout(1)
                try:
                    first = self.request.recv(64)
                except OSError:
                    first = b''
            finally:
                log({'event': 'conn', 'port': port, 'src': self.client_address[0], 'first_bytes_hex': first.hex()})

    class Smtp(Raw):
        banner = BANNER

    class Servers:
        daemon_threads = True
        allow_reuse_address = True

    class HTTPServer(Servers, http.server.ThreadingHTTPServer):
        pass

    class TCPServer(Servers, socketserver.ThreadingTCPServer):
        pass

    servers = [HTTPServer(('0.0.0.0', http_port), Web)]
    if smtp_port is not None:
        servers.append(TCPServer(('0.0.0.0', smtp_port), Smtp))
    for port in tcp_ports:
        servers.append(TCPServer(('0.0.0.0', port), Raw))
    return servers


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--log', default='/canary/tripwire-log.jsonl')
    p.add_argument('--http-port', type=int, default=8080)
    p.add_argument('--smtp-port', type=int, default=25)
    p.add_argument('--tcp', type=int, action='append', default=[])
    args = p.parse_args()
    servers = make_servers(args.log, args.http_port, args.smtp_port, args.tcp)
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print('TRIPWIRE_READY ports=%s' % ','.join(str(s.server_address[1]) for s in servers), flush=True)
    servers[0].serve_forever()


if __name__ == '__main__':
    main()
