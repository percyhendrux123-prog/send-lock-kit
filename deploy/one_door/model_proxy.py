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
# small set Hermes' Anthropic adapter may send on its own. Every field and every
# nested object is REBUILT from explicit typed keys below; nothing is copied through.
ALLOWED_FIELDS = {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice',
                  'stream', 'thinking', 'output_config', 'temperature', 'top_p', 'top_k', 'stop_sequences'}
RESPONSE_BLOCKS = {'text', 'tool_use', 'thinking', 'redacted_thinking'}
THINKING_TYPES = {'disabled', 'adaptive', 'enabled'}
EFFORTS = {'low', 'medium', 'high', 'max', 'xhigh'}
EPHEMERAL = {'type': 'ephemeral'}   # 5-minute cache only: its write price is the 1.25x the reservation assumes
OVERHEAD_TOKENS = 2000              # provider-added tool-use system prompt and framing, not in the body


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
    p.setdefault('upstream_timeout_seconds', 170)
    p.setdefault('budget_scope', 'unscoped')
    if p.pop('bytes_per_token_floor', 1) != 1:
        raise ValueError('bytes_per_token_floor must be 1 (one token per body byte, the safe bound)')
    if p['cache_write_multiplier'] < 1.25 or not 0 < p['upstream_timeout_seconds'] <= 175:
        raise ValueError('policy reservation or timeout settings invalid')
    if not isinstance(p['budget_scope'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', p['budget_scope']):
        raise ValueError('policy budget_scope invalid')
    return p


# ------------------------------------------------------------------ request building

def _keys(obj, required, optional, where):
    if not isinstance(obj, dict):
        raise Refused('%s must be an object' % where)
    extra = set(obj) - set(required) - set(optional)
    missing = set(required) - set(obj)
    if extra or missing:
        raise Refused('%s fields not allowed or missing: %s' % (where, ', '.join(sorted(extra | missing))[:120]))


def _string(value, where, limit=1_000_000):
    if not isinstance(value, str) or len(value) > limit:
        raise Refused('%s must be a string' % where)
    return value


def _cache(block, out):
    if 'cache_control' in block:
        if block['cache_control'] != EPHEMERAL:
            raise Refused('cache_control may only be {"type": "ephemeral"}')
        out['cache_control'] = dict(EPHEMERAL)
    return out


def _text_block(block, where):
    _keys(block, {'type', 'text'}, {'cache_control'}, where)
    return _cache(block, {'type': 'text', 'text': _string(block['text'], where)})


def _block(block, where):
    """Rebuild one message content block from its explicit fields. Anything else is refused."""
    kind = block.get('type') if isinstance(block, dict) else None
    if kind == 'text':
        return _text_block(block, where)
    if kind == 'tool_use':
        _keys(block, {'type', 'id', 'name', 'input'}, {'cache_control'}, where)
        if not isinstance(block['input'], dict):
            raise Refused('tool_use input must be an object')
        return _cache(block, {'type': 'tool_use', 'id': _string(block['id'], where, 128),
                              'name': _string(block['name'], where, 128), 'input': block['input']})
    if kind == 'tool_result':
        _keys(block, {'type', 'tool_use_id'}, {'content', 'is_error', 'cache_control'}, where)
        out = {'type': 'tool_result', 'tool_use_id': _string(block['tool_use_id'], where, 128)}
        content = block.get('content', '')
        if isinstance(content, list):
            out['content'] = [_text_block(b, 'tool_result block') for b in content]
        else:
            out['content'] = _string(content, 'tool_result content')
        if 'is_error' in block:
            if type(block['is_error']) is not bool:
                raise Refused('is_error must be a boolean')
            out['is_error'] = block['is_error']
        return _cache(block, out)
    if kind == 'thinking':
        _keys(block, {'type', 'thinking', 'signature'}, set(), where)
        return {'type': 'thinking', 'thinking': _string(block['thinking'], where), 'signature': _string(block['signature'], where, 100_000)}
    if kind == 'redacted_thinking':
        _keys(block, {'type', 'data'}, set(), where)
        return {'type': 'redacted_thinking', 'data': _string(block['data'], where)}
    # image, document, search_result, server_tool_use, mcp_*, container_upload ...: not needed, never forwarded
    raise Refused('content block type %r is not allowed' % str(kind)[:40])


def _schema_refs_local(node, depth=0):
    if depth > 40:
        raise Refused('tool input_schema nested too deeply')
    if isinstance(node, dict):
        ref = node.get('$ref')
        if ref is not None and not (isinstance(ref, str) and ref.startswith('#')):
            raise Refused('tool input_schema may only use local #/ references')
        for value in node.values():
            _schema_refs_local(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _schema_refs_local(value, depth + 1)


def _tools(tools):
    if not isinstance(tools, list) or len(tools) > 128:
        raise Refused('tools must be a list of at most 128 locally defined tools')
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise Refused('tool must be an object')
        if tool.get('type', 'custom') != 'custom':
            raise Refused('provider-hosted (server-side) tools are not allowed')
        _keys(tool, {'name', 'input_schema'}, {'description', 'cache_control', 'type'}, 'tool')
        if not isinstance(tool['input_schema'], dict):
            raise Refused('a local tool needs an input_schema object')
        _schema_refs_local(tool['input_schema'])
        rebuilt = {'name': _string(tool['name'], 'tool name', 128), 'input_schema': tool['input_schema']}
        if 'description' in tool:
            rebuilt['description'] = _string(tool['description'], 'tool description', 100_000)
        out.append(_cache(tool, rebuilt))
    return out


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
    rebuilt = []
    for message in messages:
        _keys(message, {'role', 'content'}, set(), 'message')
        if message['role'] not in ('user', 'assistant'):
            raise Refused('message role must be user or assistant')
        content = message['content']
        if isinstance(content, str):
            rebuilt.append({'role': message['role'], 'content': content})
        elif isinstance(content, list):
            rebuilt.append({'role': message['role'], 'content': [_block(b, 'message block') for b in content]})
        else:
            raise Refused('message content must be a string or a list of blocks')
    out = {'model': policy['model_id'], 'messages': rebuilt}
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
        system = body['system']
        if isinstance(system, str):
            out['system'] = system
        elif isinstance(system, list):
            out['system'] = [_text_block(b, 'system block') for b in system]
        else:
            raise Refused('system must be a string or a list of text blocks')
    if 'tools' in body:
        out['tools'] = _tools(body['tools'])
    if 'tool_choice' in body:
        choice = body['tool_choice']
        if not isinstance(choice, dict) or choice.get('type') not in ('auto', 'any', 'tool', 'none'):
            raise Refused('tool_choice invalid')
        _keys(choice, {'type'}, {'name', 'disable_parallel_tool_use'} if choice['type'] == 'tool' else {'disable_parallel_tool_use'}, 'tool_choice')
        rebuilt_choice = {'type': choice['type']}
        if choice['type'] == 'tool':
            rebuilt_choice['name'] = _string(choice.get('name'), 'tool_choice name', 128)
        if 'disable_parallel_tool_use' in choice:
            if type(choice['disable_parallel_tool_use']) is not bool:
                raise Refused('tool_choice invalid')
            rebuilt_choice['disable_parallel_tool_use'] = choice['disable_parallel_tool_use']
        out['tool_choice'] = rebuilt_choice
    if 'thinking' in body:
        thinking = body['thinking']
        if not isinstance(thinking, dict) or thinking.get('type') not in THINKING_TYPES:
            raise Refused('thinking invalid')
        _keys(thinking, {'type'}, {'budget_tokens', 'display'}, 'thinking')
        rebuilt_thinking = {'type': thinking['type']}
        if thinking['type'] == 'enabled':
            budget = thinking.get('budget_tokens')
            if type(budget) is not int or not 1024 <= budget < out['max_tokens']:
                raise Refused('thinking budget must be below the capped max_tokens')
            rebuilt_thinking['budget_tokens'] = budget
        if 'display' in thinking:
            if thinking['display'] not in ('summarized', 'omitted'):
                raise Refused('thinking display invalid')
            rebuilt_thinking['display'] = thinking['display']
        out['thinking'] = rebuilt_thinking
    if 'output_config' in body:
        config = body['output_config']
        if not isinstance(config, dict) or set(config) != {'effort'} or config.get('effort') not in EFFORTS:
            raise Refused('output_config may carry only effort')
        out['output_config'] = {'effort': config['effort']}
    for key, lo, hi in (('temperature', 0, 1), ('top_p', 0, 1), ('top_k', 0, 500)):
        if key in body:
            _check_number(body, key, lo, hi)
            out[key] = body[key]
    if 'stop_sequences' in body:
        stops = body['stop_sequences']
        if not isinstance(stops, list) or len(stops) > 8 or not all(isinstance(s, str) and len(s) <= 64 for s in stops):
            raise Refused('stop_sequences invalid')
        out['stop_sequences'] = list(stops)
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
    """Persisted spend ledger, one writer for the process lifetime.

    Pending reservations survive a crash as spend. The ledger is bound to one
    budget scope (the policy's), so a ledger from another scope is refused, and a
    reservation that the actual charge exceeds halts every later call."""
    def __init__(self, path, budget, scope='unscoped'):
        import fcntl
        self.path, self.budget = Path(path), float(budget)
        self._lock = open(self.path.with_name(self.path.name + '.lock'), 'a')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('another process holds this spend ledger; refusing')
        self.state = {'scope': scope, 'spent_usd': 0.0, 'pending': {}, 'calls': 0, 'case_calls': {},
                      'kept_ambiguous': 0, 'halted': None}
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text()))
            if self.state.get('scope') != scope:
                raise ValueError('spend ledger belongs to another budget scope; refusing')
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
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)  # make the rename itself durable before any dispatch
        finally:
            os.close(fd)

    def committed(self):
        return self.state['spent_usd'] + sum(self.state['pending'].values())

    def reserve(self, call_id, usd, case):
        if self.state.get('halted'):
            raise Refused('spend halted: %s' % self.state['halted'], 429)
        if self.committed() + usd > self.budget:
            raise Refused('spend cap: remaining budget cannot cover this call', 429)
        self.state['pending'][call_id] = usd
        self.state['calls'] += 1
        self.state['case_calls'][case] = self.state['case_calls'].get(case, 0) + 1
        self._save()

    def settle(self, call_id, usd):
        """usd=None keeps the full reservation (ambiguous outcome). An overrun halts the proxy."""
        reserved = self.state['pending'].pop(call_id)
        if usd is None:
            usd = reserved
            self.state['kept_ambiguous'] += 1
        elif usd > reserved:
            self.state['halted'] = 'actual charge %.6f exceeded its reservation %.6f' % (usd, reserved)
        self.state['spent_usd'] += usd
        self._save()
        return usd


def reservation_usd(upstream_bytes, max_tokens, policy):
    """Worst case: every body byte a token, plus provider overhead, all written to cache, plus full output."""
    tokens_in = upstream_bytes + OVERHEAD_TOKENS
    return (tokens_in * policy['price_input_per_mtok'] * policy['cache_write_multiplier']
            + max_tokens * policy['price_output_per_mtok']) / 1e6


def actual_usd(usage, policy):
    """None (keep the reservation) unless usage is complete, with non-negative integer counts."""
    if not isinstance(usage, dict) or 'input_tokens' not in usage or 'output_tokens' not in usage:
        return None
    counts = {}
    for key in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'):
        value = usage.get(key) or 0
        if type(value) is not int or value < 0:
            return None
        counts[key] = value
    read_price = policy.get('price_cache_read_per_mtok', policy['price_input_per_mtok'])
    return (counts['input_tokens'] * policy['price_input_per_mtok']
            + counts['cache_creation_input_tokens'] * policy['price_input_per_mtok'] * policy['cache_write_multiplier']
            + counts['cache_read_input_tokens'] * read_price
            + counts['output_tokens'] * policy['price_output_per_mtok']) / 1e6


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
    ledger = Ledger(Path(state_dir) / 'ledger.json', policy['budget_usd'], policy['budget_scope'])
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
                # A 4xx is the API refusing the request (not billed): released. A 5xx may have been
                # processed before failing: the reservation is kept.
                ledger.settle(call_id, 0.0 if 400 <= status < 500 else None)
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
