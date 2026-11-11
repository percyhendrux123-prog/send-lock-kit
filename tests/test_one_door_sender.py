#!/usr/bin/env python3
"""Fake-only sender behavior; inject delivery, never contact real services."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/one_door'))
try:
    import sender
except ImportError:
    sender = None

class SenderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(sender, 'separate sender is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ['outbox', 'approvals', 'state', 'guard', 'secrets']:
            (self.root / name).mkdir()
        (self.root / 'secrets/send-token').write_text('FAKE-DEMO-ONLY')
        self.deliveries = []
        self.config(3)
        self.draft()

    def config(self, step, **kw):
        (self.root / 'guard/axiom-owner.json').write_text(json.dumps({'step':step, **kw}))

    def draft(self, body='hello', kind='note', to='fake-owner'):
        self.raw = json.dumps({'kind':kind, 'to':to, 'body':body}).encode()
        (self.root / 'outbox/n.json').write_bytes(self.raw)

    def approve(self):
        (self.root / 'approvals/n.json').write_text(json.dumps({
            'draft_id':'n', 'sha256':hashlib.sha256(self.raw).hexdigest(), 'approved':True}))

    def run_sender(self, deliver=None):
        return sender.process(self.root, 'n.json', deliver or (lambda body, key: self.deliveries.append((body, key))))

    def test_approval_then_send_once_and_audit(self):
        self.assertEqual(self.run_sender(), 'hold')
        self.approve()
        self.assertEqual(self.run_sender(), 'send')
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(self.deliveries[0][0], self.raw)
        self.assertEqual(self.run_sender(), 'refuse')
        events = [json.loads(x)['event'] for x in (self.root/'state/audit.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['hold','attempt','send','refuse'])

    def test_edited_approval_refused(self):
        self.approve()
        self.draft(body='changed')
        self.assertEqual(self.run_sender(), 'refuse')
        self.assertEqual(self.deliveries, [])

    def test_steps_and_invalid_default(self):
        self.approve()
        for step in [0, 1, 2, -1, 5, True, '3']:
            self.config(step)
            self.assertEqual(self.run_sender(), 'refuse' if step == 1 and type(step) is int else 'hold')
        (self.root/'guard/axiom-owner.json').unlink()
        self.assertEqual(self.run_sender(), 'hold')
        (self.root/'guard/axiom-owner.json').write_text('{')
        self.assertEqual(self.run_sender(), 'hold')
        self.assertEqual(self.deliveries, [])

    def test_standing_permission_matches_pair_not_kind_alone(self):
        self.config(4, standing_permissions=[{'kind':'note','to':'fake-owner'}])
        self.assertEqual(self.run_sender(), 'send')
        self.assertEqual(len(self.deliveries), 1)
        self.draft(kind='other', to='fake-stranger')
        # Fresh id so replay protection doesn't hide permission check.
        (self.root/'outbox/m.json').write_bytes(self.raw)
        self.assertEqual(sender.process(self.root,'m.json',lambda *x: self.fail('unapproved send')), 'hold')

    def test_ambiguous_failure_never_retried(self):
        self.approve()
        def broken(*args):
            raise OSError('fake disconnect')
        self.assertEqual(self.run_sender(broken), 'refuse')
        self.assertEqual(self.run_sender(), 'refuse')
        self.assertEqual(self.deliveries, [])

    def test_untrusted_file_and_approval_forms(self):
        self.approve()
        p = self.root/'outbox/n.json'
        p.unlink()
        p.symlink_to(self.root/'secrets/send-token')
        self.assertEqual(self.run_sender(), 'refuse')
        p.unlink()
        self.draft()
        (self.root/'approvals/n.json').write_text('{')
        self.assertEqual(self.run_sender(), 'refuse')
        self.assertEqual(self.deliveries, [])

    def test_owner_cli_rechecks_snapshot_and_rejects_stale_hash(self):
        import owner
        digest = hashlib.sha256(self.raw).hexdigest()
        owner.approve(self.root, 'n.json', digest)
        self.assertEqual(self.run_sender(), 'send')
        self.draft(body='edited')
        with self.assertRaises(ValueError): owner.approve(self.root, 'n.json', digest)

    def test_concurrent_calls_deliver_once_and_snapshot_does_not_change(self):
        from concurrent.futures import ThreadPoolExecutor
        self.approve()
        original = self.raw
        def delivery(raw, token):
            self.draft(body='mutated while delivery runs')
            self.deliveries.append(raw)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_sender(delivery), range(2)))
        self.assertEqual(sorted(results), ['refuse','send'])
        self.assertEqual(self.deliveries, [original])

    def test_http_delivery_actual_fake_receiver_and_redirect_refusal(self):
        import http.server
        import threading
        seen = []
        class Fake(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                seen.append((self.path, raw, self.headers['Authorization']))
                self.send_response(302 if self.path == '/redirect' else 200)
                self.send_header('Location','/send')
                self.end_headers(); self.wfile.write(b'accepted')
            def log_message(self,*args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0), Fake)
        t = threading.Thread(target=server.serve_forever,daemon=True); t.start()
        self.addCleanup(lambda: (server.shutdown(),server.server_close(),t.join()))
        self.approve()
        base = f'http://127.0.0.1:{server.server_port}'
        self.assertEqual(self.run_sender(sender.http_delivery(base+'/send')), 'send')
        sender.http_delivery(base+'/send')(self.raw, 'FAKE-DEMO-ONLY')
        with self.assertRaises(Exception): sender.http_delivery(base+'/redirect')(self.raw, 'FAKE-DEMO-ONLY')
        self.assertEqual([x[0] for x in seen], ['/send','/send','/redirect'])
        self.assertEqual(seen[0][1:], (self.raw, 'Bearer FAKE-DEMO-ONLY'))

    def test_deeply_nested_draft_is_refused_and_logged(self):
        (self.root/'outbox/n.json').write_bytes(b'{"kind":"note","to":"fake-owner","body":'+b'['*20000+b'0'+b']'*20000+b'}')
        self.assertEqual(self.run_sender(),'refuse')
        self.assertEqual(json.loads((self.root/'state/audit.jsonl').read_text())['event'],'refuse')
        self.assertEqual(self.deliveries,[])

    def test_attempt_flushes_directory_before_delivery(self):
        from unittest.mock import patch
        import stat
        self.approve()
        syncs = []
        real_fsync = os.fsync
        def sync(fd):
            syncs.append('dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            real_fsync(fd)
        def delivery(*args):
            self.assertIn('dir',syncs)
            self.assertLess(syncs.index('file'),syncs.index('dir'))
        with patch.object(sender.os,'fsync',sync):
            self.assertEqual(self.run_sender(delivery),'send')

    def test_unreadable_queue_is_audited_without_crashing_optional_daemon(self):
        import subprocess
        outbox = self.root/'outbox'
        outbox.chmod(0)
        try:
            run = subprocess.run([sys.executable,sender.__file__,'--root',str(self.root),'--once'],capture_output=True,text=True)
        finally:
            outbox.chmod(0o755)
        self.assertEqual(run.returncode,0,run.stderr)
        event = json.loads((self.root/'state/audit.jsonl').read_text())
        self.assertEqual((event['event'],event['draft_id']),('refuse','<queue>'))

    def test_audit_failure_prevents_delivery(self):
        self.approve()
        (self.root/'state/audit.jsonl').mkdir()
        with self.assertRaises(OSError):
            self.run_sender()
        self.assertEqual(self.deliveries, [])

if __name__ == '__main__':
    unittest.main(verbosity=2)
