"""Fake Anthropic Messages endpoint for the dummy-key rehearsal. FAKE ONLY.

Stands where api.anthropic.com would be, on the rehearsal's internal egress
network, so the whole live stack (real Hermes, Anthropic provider, gateway,
model-proxy) can be exercised with no key, no spend and no internet.

Like fake_model.py it answers from the cases file: the user message carrying
[WIW-CASE:<id>] selects the script; step n = tool_result blocks after it.
It logs which top-level fields and header NAMES arrived (never header values),
so the rehearsal can prove what Hermes sends and that no caller header leaked.

Failure modes for forced-error tests, set from outside with
  python3 fake_anthropic.py set-mode --state DIR <normal|delay|stream|redirect|server-tool|status-500>
"""
import argparse
import hashlib
import http.server
import json
from pathlib import Path
import re
import threading
import time

MARKER = re.compile(r'\[WIW-CASE:([A-Za-z0-9_-]{1,40})\]')
MODES = ('normal', 'delay', 'stream', 'redirect', 'server-tool', 'status-500')
TEXT_LIMIT = 600


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
            out.append(block['text'])
        elif isinstance(block, dict) and block.get('type') == 'tool_result':
            out.append(text_of(block.get('content', '')))
    return '\n'.join(out)


def locate(messages):
    """(case, step, last tool_result block) from an Anthropic message list."""
    anchor = None
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get('role') == 'user':
            blocks = message.get('content')
            plain = blocks if isinstance(blocks, str) else '\n'.join(
                b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text') if isinstance(blocks, list) else ''
            found = MARKER.search(plain or '')
            if found:
                anchor = (index, found.group(1))
    if anchor is None:
        return None, 0, None
    results = [b for m in messages[anchor[0] + 1:] if isinstance(m, dict) and isinstance(m.get('content'), list)
               for b in m['content'] if isinstance(b, dict) and b.get('type') == 'tool_result']
    return anchor[1], len(results), (results[-1] if results else None)


def load_scripts(path):
    data = json.loads(Path(path).read_text())
    return {c['id']: c['scripted'] for c in list(data.get('cases', [])) + list(data.get('approval_flow', {}).get('agent_steps', []))}


def message(model, blocks, stop):
    return {'id': 'msg_fake_%d' % time.time_ns(), 'type': 'message', 'role': 'assistant', 'model': model,
            'content': blocks, 'stop_reason': stop, 'stop_sequence': None,
            'usage': {'input_tokens': 10, 'output_tokens': 5}}


def make_server(address, scripts, log_path, state_dir, delay_seconds=6.0):
    lock = threading.Lock()

    def log(row):
        with lock, Path(log_path).open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(row, t=time.time()), separators=(',', ':')) + '\n')

    def mode():
        try:
            value = (Path(state_dir) / 'mode').read_text().strip()
        except OSError:
            return 'normal'
        return value if value in MODES else 'normal'

    class Handler(http.server.BaseHTTPRequestHandler):
        def send(self, status, content_type, body, extra=()):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            log({'event': 'unsupported', 'method': 'GET'})
            self.send(404, 'application/json', b'{"type":"error"}')

        def do_POST(self):
            size = int(self.headers.get('Content-Length', '0') or 0)
            raw = self.rfile.read(min(size, 2 * 1024 * 1024))
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            current = mode()
            case, step, last = locate(body.get('messages') or [])
            row = {'event': 'request', 'mode': current, 'path': self.path[:80], 'case': case, 'step': step,
                   'fields': sorted(body) if isinstance(body, dict) else [],
                   'header_names': sorted(h.lower() for h in self.headers.keys()),
                   'has_api_key': bool(self.headers.get('x-api-key')),
                   'stream_field': body.get('stream') if isinstance(body, dict) else None,
                   'model': body.get('model') if isinstance(body, dict) else None, 'bytes': len(raw)}
            if last is not None:
                result = text_of(last.get('content', ''))
                row['last_tool_result'] = {'tool_use_id': str(last.get('tool_use_id', ''))[:80],
                                           'sha256': hashlib.sha256(result.encode()).hexdigest(), 'text': result[:TEXT_LIMIT]}
            log(row)
            model = str(body.get('model') or 'fake') if isinstance(body, dict) else 'fake'
            if current == 'delay':
                time.sleep(delay_seconds)
            if current == 'redirect':
                return self.send(302, 'application/json', b'{}', [('Location', 'http://elsewhere.invalid/')])
            if current == 'status-500':
                return self.send(500, 'application/json', b'{"type":"error","error":{"type":"api_error"}}')
            if current == 'stream':
                return self.send(200, 'text/event-stream', b'event: message_start\ndata: {}\n\n')
            if current == 'server-tool':
                blocks = [{'type': 'server_tool_use', 'id': 'srvtoolu_fake', 'name': 'web_search', 'input': {'query': 'x'}}]
                return self.send(200, 'application/json', json.dumps(message(model, blocks, 'end_turn')).encode())
            script = scripts.get(case) if case else None
            if script is not None and step < len(script):
                spec = script[step]
                blocks = [{'type': 'tool_use', 'id': 'toolu_wiw_%s_%d' % (case, step), 'name': spec['tool'], 'input': spec['args']}]
                answer = message(model, blocks, 'tool_use')
            else:
                answer = message(model, [{'type': 'text', 'text': ('WIW-DONE:' + case) if case else 'rehearsal ok'}], 'end_turn')
            self.send(200, 'application/json', json.dumps(answer).encode())

        def log_message(self, *args):
            pass

    return http.server.ThreadingHTTPServer(address, Handler)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd')
    s = sub.add_parser('serve')
    s.add_argument('--cases', default='/kit/wiw_cases.json')
    s.add_argument('--log', default='/fake-api/requests.jsonl')
    s.add_argument('--state', default='/fake-api')
    s.add_argument('--port', type=int, default=8080)
    m = sub.add_parser('set-mode')
    m.add_argument('--state', default='/fake-api')
    m.add_argument('mode', choices=MODES)
    args = p.parse_args()
    if args.cmd == 'set-mode':
        (Path(args.state) / 'mode').write_text(args.mode)
        return
    server = make_server(('0.0.0.0', args.port), load_scripts(args.cases), args.log, args.state)
    print('FAKE_ANTHROPIC_PORT=%d' % server.server_port, flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
