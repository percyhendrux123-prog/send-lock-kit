"""Scripted fake model for the worker-in-wall test. FAKE DATA ONLY.

An OpenAI-compatible /v1/chat/completions server that needs no credential and
costs nothing. A real Hermes runtime sends it ordinary chat requests; it answers
with the tool calls named in the cases file, one per turn, so a real Hermes tool
layer is asked to carry out each of the 46 should-block probes.

It is also an OUTSIDE OBSERVER. Every request is appended to a JSON-lines log
that the worker cannot read or write: which case, which step, what the previous
tool returned (first 600 characters). The checker reads that log, not anything
the agent says.

Case selection: the last user message must contain  [WIW-CASE:<id>].
Step n = number of tool results that followed that user message.
No marker, or no more scripted steps: plain text, no tool call.

GET /__mark/<label> appends a mark row (phase boundaries, written by the harness).
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
MARK_PATH = re.compile(r'/__mark/([A-Za-z0-9_.:-]{1,80})\Z')
MAX_BODY = 4 * 1024 * 1024
TEXT_LIMIT = 600


def flatten(content):
    """Message content may be a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get('text'), str):
                out.append(part['text'])
        return '\n'.join(out)
    return ''


def locate(messages):
    """Return (case_id, step, last_tool_message) from an OpenAI message list."""
    last_user = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get('role') == 'user':
            last_user = index
    if last_user < 0:
        return None, 0, None
    found = MARKER.search(flatten(messages[last_user].get('content')))
    if not found:
        return None, 0, None
    tools = [m for m in messages[last_user + 1:] if isinstance(m, dict) and m.get('role') == 'tool']
    return found.group(1), len(tools), (tools[-1] if tools else None)


def load_scripts(path):
    """cases file -> {case_id: [ {tool, args}, ... ]}. Approval-flow cases included."""
    data = json.loads(Path(path).read_text())
    scripts = {}
    for case in list(data.get('cases', [])) + list(data.get('approval_flow', {}).get('agent_steps', [])):
        scripts[case['id']] = case['scripted']
    return scripts


def tool_call(case, step, spec):
    return {'id': 'call_wiw_%s_%d' % (case, step), 'type': 'function',
            'function': {'name': spec['tool'], 'arguments': json.dumps(spec['args'], separators=(',', ':'))}}


def reply_body(model, call, text, stream):
    """Build the HTTP body for either a tool call or a plain text answer."""
    created = int(time.time())
    ident = 'chatcmpl-wiw-%d' % int(time.time() * 1000)
    if not stream:
        message = {'role': 'assistant', 'content': text}
        finish = 'stop'
        if call is not None:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
            finish = 'tool_calls'
        body = {'id': ident, 'object': 'chat.completion', 'created': created, 'model': model,
                'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        return 'application/json', json.dumps(body).encode()
    def chunk(delta, finish=None):
        return 'data: ' + json.dumps({'id': ident, 'object': 'chat.completion.chunk', 'created': created,
                                      'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n'
    parts = [chunk({'role': 'assistant', 'content': ''})]
    if call is not None:
        parts.append(chunk({'tool_calls': [dict(call, index=0)]}))
        parts.append(chunk({}, 'tool_calls'))
    else:
        parts.append(chunk({'content': text}))
        parts.append(chunk({}, 'stop'))
    parts.append('data: [DONE]\n\n')
    return 'text/event-stream', ''.join(parts).encode()


def make_server(address, scripts, log_path):
    lock = threading.Lock()

    def log(row):
        row = dict(row, t=time.time())
        with lock:
            with Path(log_path).open('a', encoding='utf-8') as f:
                f.write(json.dumps(row, separators=(',', ':')) + '\n')
                f.flush()

    class Handler(http.server.BaseHTTPRequestHandler):
        def send(self, status, content_type, body):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            marked = MARK_PATH.fullmatch(self.path)
            if marked:
                log({'event': 'mark', 'label': marked.group(1)})
                return self.send(200, 'application/json', b'{"fake":true}')
            if self.path == '/__health':
                return self.send(200, 'application/json', b'{"ok":true}')
            log({'event': 'unsupported', 'method': 'GET', 'path': self.path[:200]})
            self.send(404, 'application/json', b'{"error":"unsupported"}')

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                size = -1
            if not 0 <= size <= MAX_BODY:
                log({'event': 'unsupported', 'method': 'POST', 'path': self.path[:200], 'reason': 'bad length'})
                return self.send(400, 'application/json', b'{"error":"bad length"}')
            raw = self.rfile.read(size)
            if self.path.split('?', 1)[0] != '/v1/chat/completions':
                log({'event': 'unsupported', 'method': 'POST', 'path': self.path[:200], 'bytes': size})
                return self.send(404, 'application/json', b'{"error":"unsupported"}')
            try:
                request = json.loads(raw)
                messages = request.get('messages') if isinstance(request, dict) else None
                if not isinstance(messages, list):
                    raise ValueError('messages missing')
            except ValueError:
                log({'event': 'unsupported', 'method': 'POST', 'path': self.path[:200], 'bytes': size, 'reason': 'not chat JSON'})
                return self.send(400, 'application/json', b'{"error":"bad request"}')
            model = str(request.get('model') or 'wiw-scripted-1')
            stream = request.get('stream') is True
            case, step, last_tool = locate(messages)
            script = scripts.get(case) if case else None
            call, text, action = None, 'ok', 'passthrough'
            if script is not None and step < len(script):
                call, action = tool_call(case, step, script[step]), 'tool_call'
            elif script is not None:
                text, action = 'WIW-DONE:' + case, 'final'
            row = {'event': 'request', 'case': case, 'step': step, 'action': action, 'bytes': size,
                   'stream': stream, 'tools_offered': len(request.get('tools') or [])}
            if last_tool is not None:
                result = flatten(last_tool.get('content'))
                row['last_tool_result'] = {'tool_call_id': str(last_tool.get('tool_call_id', ''))[:80],
                                           'sha256': hashlib.sha256(result.encode()).hexdigest(),
                                           'text': result[:TEXT_LIMIT]}
            if call is not None:
                row['emitted'] = {'tool': script[step]['tool'],
                                  'args_sha256': hashlib.sha256(call['function']['arguments'].encode()).hexdigest()}
            log(row)
            content_type, body = reply_body(model, call, text, stream)
            self.send(200, content_type, body)

        def log_message(self, *args):
            pass  # access lines are not needed; the JSON log is the record

    return http.server.ThreadingHTTPServer(address, Handler)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cases', default='/kit/wiw_cases.json')
    p.add_argument('--log', default='/canary/model-log.jsonl')
    p.add_argument('--port', type=int, default=8080)
    args = p.parse_args()
    server = make_server(('0.0.0.0', args.port), load_scripts(args.cases), args.log)
    print('FAKE_MODEL_PORT=%d' % server.server_port, flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
