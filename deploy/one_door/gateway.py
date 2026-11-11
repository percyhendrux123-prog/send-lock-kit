"""Fixed-origin allowlist gateway, not a generic HTTP CONNECT proxy.

HTTPS is terminated here with normal certificate verification on upstream I/O;
clients use named reverse routes, so read-only methods can actually be enforced.
"""
import argparse
import http.server
import io
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from sender import NoRedirect, LIMIT, load


def target(rules, method, path):
    if not isinstance(rules, dict) or method not in {'GET','POST'}:
        return None
    if not path.startswith('/') or any(c in path for c in ('%', '\\', '#', '\r', '\n')):
        return None
    parts = urllib.parse.urlsplit(path)
    segments = parts.path.split('/')
    if len(segments) < 3 or any(s in {'.','..'} for s in segments) or '' in segments[1:]:
        return None
    rule = rules.get(segments[1])
    if not isinstance(rule, dict) or method not in rule.get('methods', []):
        return None
    origin = rule.get('origin','')
    try:
        u = urllib.parse.urlsplit(origin)
        if (u.scheme not in {'http','https'} or not u.hostname or u.username or u.password
            or u.path not in {'','/'} or u.query or u.fragment or not u.port and origin.endswith(':')):
            return None
    except ValueError:
        return None
    suffix = '/' + '/'.join(segments[2:])
    prefixes = rule.get('prefixes', [])
    if not isinstance(prefixes, list) or not any(isinstance(p,str) and p.startswith('/') and suffix.startswith(p) for p in prefixes):
        return None
    return origin.rstrip('/') + suffix + ('?' + parts.query if parts.query else '')


class DeadlineReader(io.RawIOBase):
    """Recompute remaining time per socket read, not per byte received."""
    def __init__(self, connection, seconds):
        super().__init__()
        self.connection = connection
        self.raw = connection.makefile('rb', buffering=0)
        self.deadline = time.monotonic()+seconds

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('client read deadline')
        self.connection.settimeout(remaining)
        return self.raw.readinto(buffer)

    def close(self):
        self.raw.close()
        super().close()


def server(address, rules, *, read_timeout=5.0, max_clients=16):
    if not math.isfinite(read_timeout) or read_timeout <= 0 or type(max_clients) is not int or max_clients < 1:
        raise ValueError('positive finite read deadline and positive integer client limit required')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.rfile.close()
            self.rfile = io.BufferedReader(DeadlineReader(self.connection, read_timeout))
        def answer(self, status, content):
            self.connection.settimeout(4)  # Bound response writes, independently of client reads.
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def handle_route(self):
            url = target(rules, self.command, self.path)
            if url is None:
                return self.answer(403,b'{"error":"not allowlisted"}')
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                    raise ValueError('ambiguous body')
                size = int(self.headers.get('Content-Length','0'))
                if not 0 <= size <= LIMIT or (self.command == 'GET' and size):
                    raise ValueError('invalid body size')
                raw = self.rfile.read(size) if size else None
                if raw is not None and len(raw) != size:
                    raise ValueError('short body')
                # Strip all worker headers except required application headers.
                headers = {'Content-Type':'application/json'}
                # Demo has no upstream credentials. Production must inject scoped
                # credentials here, never forward a sender token from the worker.
                request = urllib.request.Request(url, data=raw, method=self.command, headers=headers)
                try:
                    with opener.open(request,timeout=4) as response:
                        content = response.read(LIMIT+1)
                        if len(content) > LIMIT:
                            raise ValueError('upstream too large')
                        self.answer(response.status, content)
                except urllib.error.HTTPError as error:
                    error.close()
                    self.answer(502,b'{"error":"upstream refused or redirected"}')
            except (OSError, ValueError):
                self.answer(502,b'{"error":"upstream unavailable"}')
        do_GET = do_POST = do_CONNECT = do_PUT = do_DELETE = do_PATCH = do_HEAD = handle_route
        def log_message(self,*args):
            pass  # never print URLs, query values, request bodies or tokens
    class BoundedServer(http.server.ThreadingHTTPServer):
        def process_request(self, request, client_address):
            if not slots.acquire(blocking=False):
                self.shutdown_request(request)  # No waiting thread or route decision for overload.
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                slots.release()
    slots = threading.BoundedSemaphore(max_clients)
    srv = BoundedServer(address, Handler)
    return srv


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config',type=Path,default=Path('/guard/egress.json'))
    p.add_argument('--port',type=int,default=8080)
    args = p.parse_args()
    try:
        rules = load(args.config.read_bytes())
    except (OSError, ValueError):
        rules = {}
    server(('0.0.0.0',args.port),rules).serve_forever()

if __name__ == '__main__': main()
