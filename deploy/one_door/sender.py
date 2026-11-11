"""Separate sender. Owner/worker mounts are its trust boundary, not the plugin."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import urllib.request

LIMIT = 65536
ID = re.compile(r'[A-Za-z0-9_-]{1,64}\.json\Z')


def read_regular(path):
    """One bounded snapshot; no FIFOs, symlinks or hardlinks from the outbox."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_size > LIMIT:
            raise ValueError('not a bounded regular file')
        value = f.read(LIMIT + 1)
        if len(value) > LIMIT:
            raise ValueError('file too large')
        return value


def unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def load(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except RecursionError as error:
        raise ValueError('JSON nesting exceeds safe parser limit') from error


def owner_config(path):
    try:
        cfg = load(read_regular(path))
        step = cfg.get('step') if isinstance(cfg, dict) else None
        return cfg if type(step) is int and 0 <= step <= 4 else {'step':0}
    except (OSError, ValueError, UnicodeError):
        return {'step':0}


def append(log, event, draft_id, digest, reason):
    row = {'time':time.time(), 'event':event, 'draft_id':draft_id, 'sha256':digest, 'reason':reason}
    with log.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, separators=(',', ':')) + '\n')
        f.flush()
        os.fsync(f.fileno())
    # fsync(file) does not make a newly created directory entry durable. The
    # first attempt must survive a host crash before delivery can begin.
    parent = os.open(log.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def process(root, filename, deliver):
    """One draft; durable attempt prevents replay, including ambiguous delivery.

    root is an OWNER-selected tree, never a worker-supplied argument. Sender
    state must be on durable native storage. There is no automatic retry.
    """
    root = Path(root)
    log = root / 'state/audit.jsonl'
    digest = ''
    draft_id = filename[:-5] if ID.fullmatch(filename) else '<invalid>'
    with (root/'state/sender.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        def decision(event, why):
            append(log, event, draft_id, digest, why)
            return event
        cfg = owner_config(root/'guard/axiom-owner.json')
        step = cfg['step']
        if step == 0:
            return decision('hold', 'practice; sender disabled')
        if step == 1:
            return decision('refuse', 'watch; drafts not accepted')
        try:
            if draft_id == '<invalid>':
                raise ValueError('invalid draft filename')
            raw = read_regular(root/'outbox'/filename)
            digest = hashlib.sha256(raw).hexdigest()
            draft = load(raw)
            if not isinstance(draft, dict) or set(draft) != {'kind','to','body'}:
                raise ValueError('invalid draft schema')
            if any(not isinstance(v, str) or not v or len(v) > 20000 for v in draft.values()):
                raise ValueError('invalid draft fields')
            # A prior attempt, even for changed bytes, consumes this ID. A new
            # owner approval and new ID are required after an uncertain send.
            if log.exists():
                for line in log.read_text().splitlines():
                    row = load(line)
                    if not isinstance(row, dict) or 'event' not in row:
                        raise ValueError('unreadable audit history')
                    if row.get('draft_id') == draft_id and row['event'] == 'attempt':
                        return decision('refuse', 'draft ID already attempted; no retry')
            if step == 2:
                return decision('hold', 'draft step; sender never sends')
            permissions = cfg.get('standing_permissions', [])
            standing = (step == 4 and isinstance(permissions, list) and len(permissions) <= 16
                        and {'kind':draft['kind'], 'to':draft['to']} in permissions)
            if not standing:
                try:
                    approval = load(read_regular(root/'approvals'/filename))
                except FileNotFoundError:
                    return decision('hold', 'no approval')
                if (not isinstance(approval, dict) or set(approval) != {'draft_id','sha256','approved'}
                    or approval.get('approved') is not True or approval.get('draft_id') != draft_id
                    or approval.get('sha256') != digest):
                    return decision('refuse', 'approval does not match exact draft bytes')
            token = read_regular(root/'secrets/send-token').decode().strip()
            if not token:
                raise ValueError('missing sender credential')
        except (OSError, ValueError, UnicodeError):
            return decision('refuse', 'invalid input or unreadable sender state')
        # fsync before I/O. If storage fails, propagate without delivery.
        append(log, 'attempt', draft_id, digest, 'standing permission' if standing else 'exact approval')
        try:
            deliver(raw, token)
        except Exception:
            return decision('refuse', 'delivery uncertain; never retry automatically')
        return decision('send', 'receiver acknowledged')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_delivery(url):
    # Sender endpoint is owner-controlled, never supplied by a draft. No ambient
    # proxy settings, no redirects, finite timeout, no automatic retries.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def deliver(raw, token):
        req = urllib.request.Request(url, data=raw, method='POST', headers={
            'Content-Type':'application/json', 'Authorization':'Bearer ' + token})
        try:
            with opener.open(req, timeout=5) as response:
                if response.status != 200 or response.read(32) != b'accepted':
                    raise OSError('receiver did not acknowledge')
        except urllib.error.HTTPError as e:
            e.close()
            raise
    return deliver


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=Path('/data'))
    p.add_argument('--endpoint', default='http://fake-receiver:8080/send')
    p.add_argument('--once', action='store_true')
    args = p.parse_args()
    delivery = http_delivery(args.endpoint)
    while True:
        try:
            drafts = sorted((args.root/'outbox').iterdir())
        except OSError:
            # Owner may repair an unavailable queue; do not kill the optional
            # daemon or silently miss the refusal. Audit failure still stops it.
            with (args.root/'state/sender.lock').open('a') as guard:
                fcntl.flock(guard,fcntl.LOCK_EX)
                append(args.root/'state/audit.jsonl','refuse','<queue>','','outbox unavailable')
            drafts = []
        for draft in drafts:
            if draft.name.endswith('.json'):
                process(args.root, draft.name, delivery)
        if args.once:
            break
        time.sleep(2)

if __name__ == '__main__':
    main()
