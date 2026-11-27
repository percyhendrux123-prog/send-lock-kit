"""model-proxy: the only container with a route out of the wall. Stdlib only.

Accepts POST /v1/messages from the egress gateway and sends a FRESH request to
the Anthropic Messages API. Plan: docs/LIVE-MODEL-TEST-PLAN.md section 3b/3c.

  * The upstream body is rebuilt from an allowlist; any other top-level field is
    refused, never forwarded. The model id comes from the policy, not the caller.
  * Provider-hosted (server-side) tools are refused, and so is any content block
    that would make Anthropic's servers fetch something (URL image/document
    sources, MCP, containers). Only locally defined tools, which run inside the
    wall, may be offered.
  * No caller header is copied. Ambient proxy variables are ignored (http.client
    never reads them). TLS is verified against the system trust store for the
    fixed host api.anthropic.com; redirects are refused, not followed.
  * Streaming is refused in both directions: `stream: true` is refused and a
    non-JSON (event-stream) answer is treated as an upstream error.
  * The key is read from a read-only file that only this container mounts. It is
    never in an environment variable and never written to a log or an error.
    A key that is a test canary (SLKCANARY-...) can never be sent to the real API:
    it is accepted only with the rehearsal upstream, which accepts nothing else.
  * Spend: before every call the worst-case cost (input bytes as tokens, plus the
    capped output, at list price) is reserved in a persisted ledger. If the budget
    cannot cover it the call is refused. An ambiguous failure keeps its reservation.
  * One call at a time; per-case call and time caps come from a case file the
    operator writes (no open case means every call is refused).
  * Every request and response body is logged in full (restricted evidence).

Subcommands:
  serve      run the proxy (default)
  set-case   open a case window:  set-case --state DIR --case ID --max-calls N --seconds S
  close-case close the current case window
  ledger     print the spend ledger
"""
import argparse
import hashlib
import http.client
import http.server
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.parse

API_HOST = 'api.anthropic.com'
API_PATH = '/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
MAX_BODY = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
KEY_MAX = 512
CANARY_KEY = re.compile(r'SLKCANARY-[0-9a-f]{32}')
REHEARSAL_UPSTREAM = re.compile(r'http://([a-z][a-z0-9-]{0,62}):([0-9]{1,5})')  # single-label host only
CASE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,64}')

# Top-level request fields. The plan's six, plus `stream` (must be false) and the
# small set Hermes' Anthropic adapter sends on its own (validated, see _check_*).
ALLOWED_FIELDS = {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice',
                  'stream', 'thinking', 'output_config', 'temperature', 'top_p', 'top_k', 'stop_sequences'}
TOOL_FIELDS = {'name', 'description', 'input_schema', 'cache_control', 'eager_input_streaming'}
BLOCK_TYPES = {'text', 'tool_use', 'tool_result', 'thinking', 'redacted_thinking', 'image', 'document'}
LOCAL_SOURCES = {'base64', 'text'}
RESPONSE_BLOCKS = {'text', 'tool_use', 'thinking', 'redacted_thinking'}
TOOL_CHOICE_TYPES = {'auto', 'any', 'tool', 'none'}
THINKING_TYPES = {'disabled', 'adaptive', 'enabled'}
EFFORTS = {'low', 'medium', 'high', 'max', 'xhigh'}


class Refused(ValueError):
    """The caller's request is not allowed; status and a fixed message go back."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ policy

def load_policy(raw):
    """Policy is derived from the frozen run manifest; validated strictly."""
    p = json.loads(raw)
    need = {'model_id', 'max_tokens_cap', 'budget_usd', 'price_input_per_mtok', 'price_output_per_mtok'}
    if not isinstance(p, dict) or need - p.keys():
        raise ValueError('policy needs %s' % sorted(need))
    if not isinstance(p['model_id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{2,80}', p['model_id']):
        raise ValueError('policy model_id invalid')
    if type(p['max_tokens_cap']) is not int or not 1 <= p['max_tokens_cap'] <= 8192:
        raise ValueError('policy max_tokens_cap invalid')
    for key in ('budget_usd', 'price_input_per_mtok', 'price_output_per_mtok'):
        if type(p[key]) not in (int, float) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ValueError('policy %s invalid' % key)
    p.setdefault('cache_write_multiplier', 1.25)
    p.setdefault('bytes_per_token_floor', 1)
    p.setdefault('upstream_timeout_seconds', 170)
    if p['bytes_per_token_floor'] not in (1, 2) or not 0 < p['upstream_timeout_seconds'] <= 175:
        raise ValueError('policy reservation or timeout settings invalid')
    return p


# ------------------------------------------------------------------ request building

def _check_source(block):
    source = block.get('source')
    if not isinstance(source, dict) or source.get('type') not in LOCAL_SOURCES:
        raise Refused('%s blocks must carry inline data, never a URL or file reference' % block.get('type'))


def _check_blocks(blocks, where, depth=0):
    if isinstance(blocks, str):
        return
    if not isinstance(blocks, list) or depth > 2:
        raise Refused('%s content must be a string or a list of blocks' % where)
    for block in blocks:
        if not isinstance(block, dict) or block.get('type') not in BLOCK_TYPES:
            kind = block.get('type') if isinstance(block, dict) else type(block).__name__
            raise Refused('content block type %r is not allowed' % str(kind)[:40])
        if block['type'] in ('image', 'document'):
            _check_source(block)
        if block['type'] == 'tool_result':
            _check_blocks(block.get('content', ''), 'tool_result', depth + 1)


def _check_tools(tools):
    if not isinstance(tools, list) or len(tools) > 128:
        raise Refused('tools must be a list of at most 128 locally defined tools')
    for tool in tools:
        if not isinstance(tool, dict):
            raise Refused('tool must be an object')
        if 'type' in tool and tool['type'] not in (None, 'custom'):
            raise Refused('provider-hosted (server-side) tools are not allowed')
        extra = set(tool) - TOOL_FIELDS - {'type'}
        if extra:
            raise Refused('tool field(s) not allowed: %s' % ', '.join(sorted(extra))[:120])
        if not isinstance(tool.get('name'), str) or not isinstance(tool.get('input_schema'), dict):
            raise Refused('a local tool needs a name and an input_schema')


def _check_number(body, key, lo, hi):
    value = body[key]
    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
        raise Refused('%s out of range' % key)


def build_upstream(body, policy):
    """Return (fresh upstream dict, notes). Raises Refused. Never mutates `body`."""
    if not isinstance(body, dict):
        raise Refused('request body must be a JSON object')
    extra = set(body) - ALLOWED_FIELDS
    if extra:
        raise Refused('field(s) not allowed: %s' % ', '.join(sorted(extra))[:160])
    if body.get('stream') not in (None, False):
        raise Refused('streaming is not allowed')
    if 'model' in body and not isinstance(body['model'], str):
        raise Refused('model must be a string')
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages or len(messages) > 2000:
        raise Refused('messages must be a non-empty list')
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant') or set(message) - {'role', 'content'}:
            raise Refused('each message needs only a user or assistant role and content')
        _check_blocks(message.get('content'), 'message')
    out = {'model': policy['model_id'], 'messages': messages}
    notes = []
    if body.get('model') not in (None, policy['model_id']):
        notes.append('caller model %r replaced by the policy model' % str(body.get('model'))[:80])
    cap = policy['max_tokens_cap']
    asked = body.get('max_tokens', cap)
    if type(asked) is not int or asked < 1:
        raise Refused('max_tokens must be a positive integer')
    out['max_tokens'] = min(asked, cap)
    if asked > cap:
        notes.append('max_tokens %d capped to %d' % (asked, cap))
    if 'system' in body:
        if not isinstance(body['system'], (str, list)):
            raise Refused('system must be a string or a list of text blocks')
        if isinstance(body['system'], list) and any(not isinstance(b, dict) or b.get('type') != 'text' for b in body['system']):
            raise Refused('system blocks must be text')
        out['system'] = body['system']
    if 'tools' in body:
        _check_tools(body['tools'])
        out['tools'] = body['tools']
    if 'tool_choice' in body:
        choice = body['tool_choice']
        if not isinstance(choice, dict) or choice.get('type') not in TOOL_CHOICE_TYPES or set(choice) - {'type', 'name', 'disable_parallel_tool_use'}:
            raise Refused('tool_choice invalid')
        out['tool_choice'] = choice
    if 'thinking' in body:
        thinking = body['thinking']
        if not isinstance(thinking, dict) or thinking.get('type') not in THINKING_TYPES or set(thinking) - {'type', 'budget_tokens', 'display'}:
            raise Refused('thinking invalid')
        if thinking['type'] == 'enabled':
            budget = thinking.get('budget_tokens')
            if type(budget) is not int or not 1024 <= budget < out['max_tokens']:
                raise Refused('thinking budget must be below the capped max_tokens')
        out['thinking'] = thinking
    if 'output_config' in body:
        config = body['output_config']
        if not isinstance(config, dict) or set(config) - {'effort'} or config.get('effort') not in EFFORTS:
            raise Refused('output_config may carry only effort')
        out['output_config'] = config
    for key, lo, hi in (('temperature', 0, 1), ('top_p', 0, 1), ('top_k', 0, 500)):
        if key in body:
            _check_number(body, key, lo, hi)
            out[key] = body[key]
    if 'stop_sequences' in body:
        stops = body['stop_sequences']
        if not isinstance(stops, list) or len(stops) > 8 or not all(isinstance(s, str) and len(s) <= 64 for s in stops):
            raise Refused('stop_sequences invalid')
        out['stop_sequences'] = stops
    return out, notes


def check_response(raw, content_type):
    """Upstream answer must be one non-streamed JSON message with known block types."""
    if 'event-stream' in (content_type or '') or not (content_type or '').startswith('application/json'):
        raise ValueError('upstream answer is not a single JSON message')
    message = json.loads(raw)
    if not isinstance(message, dict) or message.get('type') != 'message' or not isinstance(message.get('content'), list):
        raise ValueError('upstream answer is not a message object')
    for block in message['content']:
        if not isinstance(block, dict) or block.get('type') not in RESPONSE_BLOCKS:
            raise ValueError('upstream answer has a block type outside the allowlist')
    return message


# ------------------------------------------------------------------ key file

def read_key(path):
    """Read-only regular file, not a symlink, no write bits, small, one line."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o222 or st.st_size > KEY_MAX:
            raise ValueError('key file must be a small regular file with no write permission')
        value = f.read(KEY_MAX + 1).decode('ascii').strip()
    if not value or any(c.isspace() for c in value) or len(value) > KEY_MAX:
        raise ValueError('key file content is not a single token')
    return value


# ------------------------------------------------------------------ ledger

class Ledger:
    """Persisted spend ledger. Pending reservations survive a crash as spend."""
    def __init__(self, path, budget):
        self.path, self.budget = Path(path), float(budget)
        self.state = {'spent_usd': 0.0, 'pending': {}, 'calls': 0, 'case_calls': {}, 'kept_ambiguous': 0}
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text()))
        if self.state['pending']:
            # A reservation left behind by a crash or kill: the call may have been billed.
            for value in self.state['pending'].values():
                self.state['spent_usd'] += value
                self.state['kept_ambiguous'] += 1
            self.state['pending'] = {}
            self._save()

    def _save(self):
        tmp = self.path.with_name(self.path.name + '.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(self.state, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def committed(self):
        return self.state['spent_usd'] + sum(self.state['pending'].values())

    def reserve(self, call_id, usd, case):
        if self.committed() + usd > self.budget:
            raise Refused('spend cap: remaining budget cannot cover this call', 429)
        self.state['pending'][call_id] = usd
        self.state['calls'] += 1
        self.state['case_calls'][case] = self.state['case_calls'].get(case, 0) + 1
        self._save()

    def settle(self, call_id, usd):
        """usd=None keeps the full reservation (ambiguous outcome)."""
        reserved = self.state['pending'].pop(call_id)
        if usd is None:
            usd = reserved
            self.state['kept_ambiguous'] += 1
        self.state['spent_usd'] += usd
        self._save()
        return usd


def reservation_usd(upstream_bytes, max_tokens, policy):
    tokens_in = upstream_bytes / policy['bytes_per_token_floor']
    return (tokens_in * policy['price_input_per_mtok'] * policy['cache_write_multiplier']
            + max_tokens * policy['price_output_per_mtok']) / 1e6


def actual_usd(usage, policy):
    if not isinstance(usage, dict):
        return None
    try:
        tin = int(usage.get('input_tokens', 0))
        tout = int(usage.get('output_tokens', 0))
        cw = int(usage.get('cache_creation_input_tokens') or 0)
        cr = int(usage.get('cache_read_input_tokens') or 0)
    except (TypeError, ValueError):
        return None
    read_price = policy.get('price_cache_read_per_mtok', policy['price_input_per_mtok'])
    return (tin * policy['price_input_per_mtok'] + cw * policy['price_input_per_mtok'] * policy['cache_write_multiplier']
            + cr * read_price + tout * policy['price_output_per_mtok']) / 1e6


# ------------------------------------------------------------------ case window

def read_case(state_dir):
    try:
        case = json.loads((Path(state_dir) / 'case.json').read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(case, dict) or not isinstance(case.get('case'), str) or not CASE_ID.fullmatch(case['case'])
            or type(case.get('max_calls')) is not int or type(case.get('deadline')) not in (int, float)):
        return None
    return case


def write_case(state_dir, case_id, max_calls, seconds):
    if not CASE_ID.fullmatch(case_id) or not 1 <= max_calls <= 200 or not 0 < seconds <= 3600:
        raise ValueError('case id, max calls (1-200) or seconds (1-3600) invalid')
    path = Path(state_dir) / 'case.json'
    tmp = path.with_name('case.json.tmp')
    tmp.write_text(json.dumps({'case': case_id, 'max_calls': max_calls, 'opened': time.time(),
                               'deadline': time.time() + seconds}))
    os.replace(tmp, path)


def close_case(state_dir):
    try:
        (Path(state_dir) / 'case.json').unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ upstream

class Upstream:
    """One HTTPS (or rehearsal HTTP) endpoint. No proxies, no redirects followed."""
    def __init__(self, host, port, context=None, *, plain=False):
        self.host, self.port, self.context, self.plain = host, port, context, plain

    def post(self, body, headers, timeout):
        if self.plain:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        else:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=self.context)
        try:
            conn.request('POST', API_PATH, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('upstream answer too large')
            return response.status, response.getheader('Content-Type', ''), raw
        finally:
            conn.close()


def production_upstream():
    context = ssl.create_default_context()  # system trust store, hostname checked
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return Upstream(API_HOST, 443, context)


def choose_upstream(key, rehearsal_url):
    """Production unless a rehearsal URL is given; a canary key is never sent to the real API."""
    if rehearsal_url is None:
        if CANARY_KEY.fullmatch(key):
            raise ValueError('refusing: a test canary key cannot be sent to the real API')
        return production_upstream()
    m = REHEARSAL_UPSTREAM.fullmatch(rehearsal_url)
    if not m or not CANARY_KEY.fullmatch(key):
        raise ValueError('rehearsal upstream needs a single-label http host and a SLKCANARY dummy key')
    return Upstream(m.group(1), int(m.group(2)), plain=True)


# ------------------------------------------------------------------ server

def make_server(address, *, policy, key, upstream, log_path, state_dir, allow_from=None):
    policy = load_policy(json.dumps(policy))  # defaults and validation apply to every caller
    ledger = Ledger(Path(state_dir) / 'ledger.json', policy['budget_usd'])
    key_bytes = key.encode()
    log_lock = threading.Lock()
    call_lock = threading.Lock()

    def scrub(value):
        text = value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))
        return text.replace(key, '[KEY]')

    def log(row):
        row = dict(row, t=time.time())
        line = scrub(row)
        if key_bytes in line.encode():  # belt and braces; scrub() already replaced it
            line = json.dumps({'t': row['t'], 'event': 'log-redacted'})
        with log_lock, Path(log_path).open('a', encoding='utf-8') as f:
            f.write(line + '\n')
            f.flush()
            os.fsync(f.fileno())

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def reply(self, status, payload):
            data = scrub(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def error(self, status, kind, message):
            self.reply(status, {'type': 'error', 'error': {'type': kind, 'message': message}})

        def refuse_method(self):
            log({'event': 'refused', 'method': self.command, 'path': self.path[:200], 'reason': 'method or path'})
            self.error(404, 'not_found_error', 'only POST /v1/messages is served')
        do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_CONNECT = do_OPTIONS = refuse_method

        def do_POST(self):
            self.connection.settimeout(10)
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != API_PATH or parts.query not in ('', 'beta=true'):
                return self.refuse_method()
            if allow_from and self.client_address[0] not in allow_from:
                log({'event': 'refused', 'reason': 'source not allowed', 'src': self.client_address[0]})
                return self.error(403, 'permission_error', 'source not allowed')
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise Refused('a single Content-Length is required')
                size = int(self.headers['Content-Length'])
                if not 0 < size <= MAX_BODY:
                    raise Refused('body size out of range', 413)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise Refused('short body')
            except (Refused, ValueError) as error:
                status = error.status if isinstance(error, Refused) else 400
                log({'event': 'refused', 'reason': str(error)})
                return self.error(status, 'invalid_request_error', str(error))
            if not call_lock.acquire(timeout=1):
                log({'event': 'refused', 'reason': 'concurrency 1', 'bytes': size})
                return self.error(429, 'rate_limit_error', 'one model call at a time')
            try:
                self.dispatch(raw)
            finally:
                call_lock.release()

        def dispatch(self, raw):
            case = read_case(state_dir)
            try:
                body = json.loads(raw)
            except ValueError:
                log({'event': 'refused', 'reason': 'not JSON', 'request_raw': raw.decode('utf-8', 'replace')})
                return self.error(400, 'invalid_request_error', 'body is not JSON')
            base = {'case': case['case'] if case else None, 'request': body}
            if case is None:
                log(dict(base, event='refused', reason='no open case window'))
                return self.error(429, 'rate_limit_error', 'no open case window')
            if time.time() >= case['deadline'] or ledger.state['case_calls'].get(case['case'], 0) >= case['max_calls']:
                log(dict(base, event='cap', reason='per-case call or time cap reached'))
                return self.error(429, 'rate_limit_error', 'case cap reached')
            try:
                upstream_body, notes = build_upstream(body, policy)
            except Refused as error:
                log(dict(base, event='refused', reason=str(error)))
                return self.error(error.status, 'invalid_request_error', str(error))
            data = json.dumps(upstream_body, separators=(',', ':')).encode()
            call_id = hashlib.sha256(data + str(time.time_ns()).encode()).hexdigest()[:16]
            reserve = reservation_usd(len(data), upstream_body['max_tokens'], policy)
            try:
                ledger.reserve(call_id, reserve, case['case'])
            except Refused as error:
                log(dict(base, event='cap', reason=str(error), reserve_usd=reserve))
                return self.error(error.status, 'rate_limit_error', str(error))
            headers = {'content-type': 'application/json', 'anthropic-version': ANTHROPIC_VERSION,
                       'x-api-key': key, 'content-length': str(len(data))}
            row = dict(base, event='call', call_id=call_id, notes=notes, upstream_request=upstream_body,
                       reserve_usd=reserve)
            log(dict(row, event='dispatch'))
            try:
                status, content_type, answer = upstream.post(data, headers, policy['upstream_timeout_seconds'])
            except (OSError, ssl.SSLError, http.client.HTTPException, ValueError, socket.timeout) as error:
                spent = ledger.settle(call_id, None)
                log(dict(row, event='upstream-error', error=type(error).__name__, charged_usd=spent, ambiguous=True))
                return self.error(504 if isinstance(error, (socket.timeout, TimeoutError)) else 502,
                                  'api_error', 'upstream unavailable')
            text = answer.decode('utf-8', 'replace')
            if 300 <= status < 400:
                ledger.settle(call_id, 0.0)
                log(dict(row, event='upstream-error', status=status, error='redirect refused', response_raw=text))
                return self.error(502, 'api_error', 'upstream redirected; refused')
            if status != 200:
                # A definite error status from the API is not billed; the reservation is released.
                ledger.settle(call_id, 0.0)
                log(dict(row, event='upstream-status', status=status, response_raw=text))
                return self.error(502 if status >= 500 else 400, 'api_error', 'upstream returned %d' % status)
            try:
                message = check_response(answer, content_type)
            except ValueError as error:
                spent = ledger.settle(call_id, None)
                log(dict(row, event='upstream-error', status=status, error=str(error), response_raw=text,
                         charged_usd=spent, ambiguous=True))
                return self.error(502, 'api_error', 'upstream answer refused')
            cost = actual_usd(message.get('usage'), policy)
            spent = ledger.settle(call_id, cost)
            log(dict(row, event='call', status=status, response=message, model_returned=message.get('model'),
                     usage=message.get('usage'), charged_usd=spent, ambiguous=cost is None))
            self.reply(200, message)

        def log_message(self, *args):
            pass  # the JSON log is the record

    class Serial(http.server.HTTPServer):
        request_queue_size = 4

    server = Serial(address, Handler)
    server.ledger = ledger
    return server


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd')
    s = sub.add_parser('serve')
    s.add_argument('--policy', type=Path, default=Path('/kit/proxy-policy.json'))
    s.add_argument('--key-file', type=Path, default=Path('/run/key/key'))
    s.add_argument('--log', type=Path, default=Path('/var/proxy/transcript.jsonl'))
    s.add_argument('--state', type=Path, default=Path('/var/proxy'))
    s.add_argument('--port', type=int, default=8080)
    s.add_argument('--allow-from', action='append', default=None, help='source IP allowed (repeatable)')
    s.add_argument('--rehearsal-upstream', default=None, help='http://<single-label-host>:<port>; dummy key only')
    c = sub.add_parser('set-case')
    c.add_argument('--state', type=Path, default=Path('/var/proxy'))
    c.add_argument('--case', required=True)
    c.add_argument('--max-calls', type=int, required=True)
    c.add_argument('--seconds', type=float, required=True)
    x = sub.add_parser('close-case')
    x.add_argument('--state', type=Path, default=Path('/var/proxy'))
    g = sub.add_parser('ledger')
    g.add_argument('--state', type=Path, default=Path('/var/proxy'))
    args = p.parse_args(argv)
    if args.cmd == 'set-case':
        write_case(args.state, args.case, args.max_calls, args.seconds)
        return 0
    if args.cmd == 'close-case':
        close_case(args.state)
        return 0
    if args.cmd == 'ledger':
        print((args.state / 'ledger.json').read_text())
        return 0
    if args.cmd != 'serve':
        p.print_help()
        return 2
    try:
        policy = load_policy(args.policy.read_bytes())
        key = read_key(args.key_file)
        upstream = choose_upstream(key, args.rehearsal_upstream)
    except (OSError, ValueError, UnicodeError) as error:
        # Our own ValueError texts never carry file content; decode and OS errors might, so only their type prints.
        detail = str(error) if type(error) is ValueError else type(error).__name__
        print('model-proxy refused to start: %s' % detail, file=sys.stderr)
        return 2
    server = make_server(('0.0.0.0', args.port), policy=policy, key=key, upstream=upstream,
                         log_path=args.log, state_dir=args.state, allow_from=set(args.allow_from or []) or None)
    print('MODEL_PROXY_PORT=%d mode=%s' % (server.server_port, 'rehearsal' if args.rehearsal_upstream else 'production'), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
