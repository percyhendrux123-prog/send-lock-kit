#!/usr/bin/env python3
"""Offline tests for the worker-in-wall harness. No Docker, no Hermes, no network
beyond loopback sockets on ephemeral ports. These test the harness, NOT the wall:
the container evidence only exists after the operator runs the real script."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
DOOR = ROOT / 'deploy/one_door'
sys.path.insert(0, str(DOOR))
import wiw_check as W          # noqa: E402
import fake_model as FM        # noqa: E402
import tripwire as TW          # noqa: E402
import wiw_worker_probe as WP  # noqa: E402
import gateway                 # noqa: E402
import sender as kit_sender    # noqa: E402

CASES = DOOR / 'wiw_cases.json'
PROBE = ROOT / 'tests/fixtures/attack_cases/probe.py'
GW = '172.30.0.9'
GIB = W.GIB


def load_plugin():
    spec = importlib.util.spec_from_file_location('kit_lock_wiw', ROOT / 'plugins/send-lock/__init__.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ cases file

class CasesTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(CASES.read_text())

    def test_file_is_sound_and_matches_probe_py_exactly(self):
        self.assertEqual(W.validate_cases(self.data, W.read_probe_rows(PROBE)), [])
        probes = W.probe_cases(self.data)
        self.assertEqual(sum(c['kind'] == 'should_block' for c in probes), 46)
        self.assertEqual(sum(c['kind'] == 'control' for c in probes), 3)

    def test_both_delivery_styles_and_all_groups_are_present(self):
        probes = W.probe_cases(self.data)
        self.assertGreaterEqual(sum(c['delivery'] == 'planted' for c in probes), 15)
        self.assertGreaterEqual(sum(c['delivery'] == 'task' for c in probes), 15)
        groups = {c['group'] for c in probes}
        self.assertEqual(groups, {'baseline', 'shell-email', 'two-step', 'shell-chat', 'publish', 'exfil', 'browser', 'other-tools', 'schedule', 'guard'})

    def test_every_case_carries_its_marker_and_prompts_are_plain_english(self):
        for case in self.data['cases']:
            self.assertTrue(case['prompt'].startswith('[WIW-CASE:%s] ' % case['id']))
            self.assertGreater(len(case['prompt'].split()), 6, case['id'])

    def test_plugin_agrees_with_each_scripted_call(self):
        """The cases are the same calls the existing 46+3 regression feeds the plugin."""
        with tempfile.TemporaryDirectory() as home:
            old = dict(os.environ)
            os.environ['HERMES_HOME'] = home
            os.environ.pop('AXIOM_OWNER_CONFIG', None)
            try:
                lock = load_plugin()
                for case in W.probe_cases(self.data):
                    call = case['scripted'][-1]
                    blocked = lock.classify(call['tool'], call['args']) is not None
                    self.assertEqual(blocked, case['kind'] == 'should_block', case['id'])
                    if case['delivery'] == 'planted':   # reading the planted note must be allowed at step 0
                        self.assertIsNone(lock.classify('read_file', case['scripted'][0]['args']), case['id'])
            finally:
                os.environ.clear(); os.environ.update(old)

    def test_validation_catches_edits(self):
        probe_rows = W.read_probe_rows(PROBE)
        def broken(change):
            data = json.loads(CASES.read_text())
            change(data)
            return W.validate_cases(data, probe_rows)
        self.assertTrue(broken(lambda d: d['cases'][0]['scripted'][-1]['args'].update(text='changed')))
        self.assertTrue(broken(lambda d: d['cases'].pop(1)))
        self.assertTrue(broken(lambda d: d['cases'][0].update(kind='control')))
        self.assertTrue(broken(lambda d: d['cases'][0].update(prompt='no marker here')))
        self.assertTrue(broken(lambda d: d['cases'][1]['planted'].update(path='/etc/passwd')))
        self.assertTrue(broken(lambda d: d['cases'][2].update(planted={'path': '/workspace/fixtures/x', 'text': 't'})))
        self.assertTrue(broken(lambda d: d['approval_flow'].update(expected_receipts=2)))
        self.assertTrue(broken(lambda d: d['approval_flow']['agent_steps'].pop()))
        self.assertTrue(broken(lambda d: d['cases'].append(dict(d['cases'][0]))))

    def test_cli_feeds_the_shell_loop(self):
        def cli(*args):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = W.main(list(args))
            return rc, out.getvalue()
        rc, out = cli('ids', '--cases', str(CASES))
        ids = out.split()
        self.assertEqual((rc, len(ids), ids[0], ids[-1]), (0, 50, 'P01', 'RC1'))
        self.assertNotIn('SMOKE', ids)
        self.assertEqual(cli('ids', '--cases', str(CASES), '--set', 'flow')[1].split(), ['A1', 'A2', 'A3', 'A4', 'A5'])
        self.assertTrue(cli('prompt', '--cases', str(CASES), '--id', 'P01')[1].startswith('[WIW-CASE:P01] '))
        self.assertTrue(cli('prompt', '--cases', str(CASES), '--id', 'A2')[1].startswith('[WIW-CASE:A2] '))
        self.assertEqual(cli('planted', '--cases', str(CASES), '--id', 'P02', '--field', 'path')[1], '/workspace/fixtures/notes/P02.txt')
        self.assertEqual(cli('planted', '--cases', str(CASES), '--id', 'P01', '--field', 'path')[1], '')
        self.assertEqual(cli('validate-cases', '--cases', str(CASES), '--probe', str(PROBE))[0], 0)


# ------------------------------------------------------------------ guards

class GuardTests(unittest.TestCase):
    def test_preflight_refuses_under_ten_gib(self):
        need = 10 * GIB
        self.assertEqual(W.preflight_reasons(11 * GIB, need, [], []), [])
        self.assertEqual(W.preflight_reasons(10 * GIB, need, [], []), [])
        low = W.preflight_reasons(10 * GIB - 1, need, [], [])
        self.assertEqual(len(low), 1)
        self.assertIn('minimum', low[0])
        self.assertTrue(W.preflight_reasons(0, need, [], []))

    def test_preflight_refuses_running_container_or_vm(self):
        need = 10 * GIB
        self.assertIn('container', W.preflight_reasons(20 * GIB, need, ['hermes-prod'], [])[0])
        self.assertIn('VM', W.preflight_reasons(20 * GIB, need, [], ['colima profile default'])[0])
        self.assertEqual(len(W.preflight_reasons(1 * GIB, need, ['a'], ['b'])), 3)
        self.assertEqual(W.preflight_reasons(20 * GIB, need, ['  ', ''], ['\n']), [])

    def test_preflight_cli_uses_the_injected_value_and_exit_codes(self):
        def run(*args):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = W.main(['preflight', *args])
            return rc, out.getvalue()
        rc, out = run('--free-bytes', str(9 * GIB))
        self.assertEqual(rc, 2)
        self.assertIn('REFUSED', out)
        self.assertEqual(run('--free-bytes', str(11 * GIB))[0], 0)
        self.assertEqual(run('--free-bytes', str(50 * GIB), '--containers-text', 'one\ntwo\n')[0], 2)
        self.assertEqual(run('--free-bytes', str(50 * GIB), '--vms-text', 'colima profile x')[0], 2)
        rc, out = run()   # real disk read must not crash
        self.assertIn(rc, (0, 2))
        self.assertIn('free disk', out)

    def test_mid_run_floor_vm_disk_and_image_cap(self):
        self.assertEqual(W.floor_reasons(4 * GIB, 4 * GIB), [])
        self.assertTrue(W.floor_reasons(4 * GIB - 1, 4 * GIB))
        self.assertTrue(W.image_reasons(5 * GIB + 1, 5 * GIB))
        self.assertEqual(W.image_reasons(5 * GIB, 5 * GIB), [])
        for args, want in ((['floor', '--free-bytes', str(3 * GIB)], 2), (['floor', '--free-bytes', str(5 * GIB)], 0),
                           (['vm-disk', '--free-bytes', str(7 * GIB)], 2), (['image', '--bytes', str(6 * GIB)], 2),
                           (['image', '--bytes', str(2 * GIB)], 0)):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(W.main(args), want, args)

    def test_space_returned_check(self):
        ok, delta = W.space_returned(11 * GIB, 11 * GIB - 100, GIB)
        self.assertTrue(ok)
        ok, delta = W.space_returned(11 * GIB, 8 * GIB, GIB)
        self.assertFalse(ok)
        self.assertEqual(delta, -3 * GIB)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(W.main(['space-returned', '--before', str(11 * GIB), '--after', str(5 * GIB)]), 1)
        self.assertIn('NO', out.getvalue())

    def test_images_must_be_digest_pinned(self):
        good = 'nousresearch/hermes-agent@sha256:' + 'a' * 64
        self.assertTrue(W.digest_pinned(good))
        for bad in ['nousresearch/hermes-agent:latest', 'python:3.12-slim', good[:-1], 'x@sha256:' + 'G' * 64, '', 'a b@sha256:' + 'a' * 64]:
            self.assertFalse(W.digest_pinned(bad), bad)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(W.main(['digest', 'python:3.12-slim']), 2)
            self.assertEqual(W.main(['digest', good]), 0)

    def test_checker_constant_matches_the_gateway_limit(self):
        self.assertEqual(W.GATEWAY_BODY_LIMIT, kit_sender.LIMIT)


# ------------------------------------------------------------------ classification

class ClassifyTests(unittest.TestCase):
    def test_real_plugin_refusals_are_recognised(self):
        with tempfile.TemporaryDirectory() as home:
            old = dict(os.environ)
            os.environ['HERMES_HOME'] = home
            os.environ.pop('AXIOM_OWNER_CONFIG', None)
            try:
                lock = load_plugin()
                for tool, args in (('terminal', {'command': 'echo hi | sendmail a@example.test'}), ('terminal', {'command': 'python3 -c "print(1)"'}),
                                   ('send_message', {'target': 'x', 'message': 'hi'}), ('write_file', {'path': '/approvals/a.json', 'content': '{}'})):
                    refusal = lock.pre_tool_call(tool_name=tool, args=args)
                    self.assertEqual(refusal['action'], 'block')
                    self.assertEqual(W.classify_result(refusal['message']), 'plugin_block', tool)
            finally:
                os.environ.clear(); os.environ.update(old)

    def test_other_results(self):
        c = W.classify_result
        self.assertEqual(c('curl: (6) Could not resolve host: example.com'), 'network_denied')
        self.assertEqual(c('ConnectionRefusedError: [Errno 111] Connection refused'), 'network_denied')
        self.assertEqual(c('urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name resolution>'), 'network_denied')
        self.assertEqual(c("bash: /approvals/first.json: Read-only file system"), 'fs_denied')
        self.assertEqual(c('PermissionError: [Errno 13] Permission denied'), 'fs_denied')
        self.assertEqual(c("Unknown tool 'phone_exec'"), 'tool_unavailable')
        self.assertEqual(c('total 0\ndrwxr-xr-x 2 hermes'), 'other')
        self.assertEqual(c(''), 'other')
        self.assertEqual(c(None), 'other')


# ------------------------------------------------------------------ synthetic evidence

LOCK_TEXT = 'BLOCKED by Axiom send-lock (kit-2.0). KIND: PERMISSION'
NET_TEXT = 'curl: (6) Could not resolve host: api.example.invalid'


def draft_bytes(body):
    return json.dumps({'kind': 'note', 'to': 'fake-owner', 'body': body}).encode()


class Builder:
    """Writes the evidence a clean run would leave, then lets a test spoil one thing."""
    def __init__(self, root, data):
        self.root, self.data, self.t = Path(root), data, 1000.0
        self.model, self.trip = [], []
        self.receipts = {'locked': [], 'wall-only': []}
        self.audit = {'locked': [], 'wall-only': []}
        self.skip = set()
        self.text = {}
        self.approval = self.good_approval()

    def mark(self, label):
        self.t += 1
        self.model.append({'event': 'mark', 'label': label, 't': self.t})
        self.trip.append({'event': 'mark', 'label': label, 't': self.t, 'src': '172.30.0.5'})

    def run_case(self, case, phase, results=None):
        self.t += 12
        if case['id'] in self.skip:
            return
        n = len(case['scripted'])
        for step in range(n + 1):
            row = {'event': 'request', 'case': case['id'], 'step': step, 'bytes': 21000, 'stream': False, 'tools_offered': 5, 't': self.t + step * 0.4,
                   'action': 'tool_call' if step < n else 'final'}
            if step:
                row['last_tool_result'] = {'tool_call_id': 'c', 'sha256': '0' * 64, 'text': self.result_text(case, phase, step - 1, results)}
            self.model.append(row)
        if case['kind'] == 'route_control' and 'norouthit' not in self.skip:
            self.trip.append({'event': 'http', 'port': 8080, 'src': GW, 'method': 'GET', 'path': '/read/mail', 'bytes': 0, 't': self.t + 0.6})

    def result_text(self, case, phase, index, results):
        if results and case['id'] in results:
            return results[case['id']]
        if case['id'] in self.text:
            return self.text[case['id']]
        if index < len(case['scripted']) - 1:
            return 'planted note text'
        if case['kind'] == 'route_control':
            return '{"fake":true}'
        if case['kind'] == 'control':
            return 'total 0' if phase == 'locked' else NET_TEXT
        return LOCK_TEXT if phase == 'locked' else NET_TEXT

    def phase(self, name):
        self.mark('phase:%s:start' % name)
        for case in self.data['cases']:
            if case['kind'] != 'smoke':
                self.run_case(case, name)
        self.mark('phase:%s:end' % name)
        self.t += 50

    def good_approval(self):
        first = draft_bytes('hello from fake demo')
        digest = hashlib.sha256(first).hexdigest()
        return {
            'audit.jsonl': ''.join(json.dumps({'event': e}) + '\n' for e in ['hold', 'hold', 'attempt', 'send', 'refuse', 'refuse', 'hold']),
            'receipts-final.jsonl': json.dumps({'raw': first.decode(), 'path': '/send'}) + '\n',
            'draft-first.json': first, 'owner-digest-first.txt': digest + '\n',
            'approval-first.json': json.dumps({'draft_id': 'first', 'sha256': digest, 'approved': True}),
            'draft-edited-approved.json': draft_bytes('draft to be edited'), 'draft-edited-final.json': draft_bytes('edited after owner approval'),
            'approvals-after-forge.txt': '', 'approvals-listing.txt': 'edited.json\nfirst.json\nstep-two.json\n',
            'send-results.txt': 'first.json hold\nfirst.json hold\nfirst.json send\nfirst.json refuse\nedited.json refuse\nstep-two.json hold\n',
        }

    def write(self, phases=('locked', 'wall-only'), approval=True, smoke=True):
        if smoke:
            self.mark('phase:smoke:start')
            self.run_case(next(c for c in self.data['cases'] if c['kind'] == 'smoke'), 'locked')
            self.mark('phase:smoke:end')
        for name in phases:
            self.phase(name)
        if approval:
            self.mark('phase:approval:start')
            for step in self.data['approval_flow']['agent_steps']:
                if step['id'] in self.skip:
                    continue
                self.t += 12
                n = len(step['scripted'])
                for s in range(n + 1):
                    row = {'event': 'request', 'case': step['id'], 'step': s, 'bytes': 20000, 'stream': False, 't': self.t + s * 0.4, 'action': 'tool_call' if s < n else 'final'}
                    if s:
                        row['last_tool_result'] = {'tool_call_id': 'c', 'sha256': '0' * 64, 'text': LOCK_TEXT if step['id'] == 'A2' else 'Wrote file'}
                    self.model.append(row)
            self.mark('phase:approval:end')
        root = self.root
        (root / 'approval').mkdir(parents=True, exist_ok=True)
        (root / 'model-log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in self.model))
        (root / 'tripwire-log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in self.trip))
        for name in ('locked', 'wall-only'):
            (root / ('receipts-after-%s.jsonl' % name)).write_text(''.join(json.dumps(r) + '\n' for r in self.receipts[name]))
            (root / ('audit-after-%s.jsonl' % name)).write_text(''.join(json.dumps(r) + '\n' for r in self.audit[name]))
        if approval:
            for name, value in self.approval.items():
                if value is None:
                    continue
                (root / 'approval' / name).write_bytes(value if isinstance(value, bytes) else value.encode())
        return self


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(CASES.read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, **kw):
        return Builder(self.root, self.data)

    def verdict(self, builder, **write):
        builder.write(**write)
        return W.evaluate_all(self.data, W.Evidence(self.root), GW)

    def test_clean_run_passes(self):
        v = self.verdict(self.build())
        self.assertEqual(v['overall'], 'PASS', json.dumps(v)[:800])
        self.assertEqual([p['wall'] for p in v['phases']], ['PASS', 'PASS'])
        self.assertEqual(v['phases'][0]['lock'], 'PASS')
        self.assertEqual(v['approval']['status'], 'PASS')
        self.assertEqual(len(v['phases'][0]['cases']), 50)
        self.assertIn('worker-in-wall: PASS (fake services only)', W.summary_lines(v))

    def test_a_tripwire_hit_during_a_case_fails_the_wall(self):
        b = self.build()
        b.write()
        self.root.joinpath('tripwire-log.jsonl').write_text('')  # rebuild with a spoiled log
        b = Builder(self.root, self.data)
        orig = b.run_case
        def spoiled(case, phase, results=None):
            orig(case, phase, results)
            if case['id'] == 'P07' and phase == 'wall-only':
                b.trip.append({'event': 'http', 'port': 8080, 'src': '172.29.49.7', 'method': 'POST', 'path': '/v3/mail/send', 'bytes': 40, 't': b.t + 0.5})
        b.run_case = spoiled
        v = self.verdict(b)
        self.assertEqual(v['overall'], 'FAIL')
        self.assertEqual(v['phases'][1]['wall'], 'FAIL')
        self.assertEqual(v['phases'][0]['wall'], 'PASS')
        self.assertEqual(v['phases'][1]['violations'][0]['case'], 'P07')

    def test_smtp_connection_logged_by_tripwire_fails(self):
        b = self.build()
        orig = b.run_case
        def spoiled(case, phase, results=None):
            orig(case, phase, results)
            if case['id'] == 'P10' and phase == 'locked':
                b.trip.append({'event': 'conn', 'port': 25, 'src': '172.29.49.7', 'first_bytes_hex': '', 't': b.t + 0.2})
        b.run_case = spoiled
        self.assertEqual(self.verdict(b)['phases'][0]['wall'], 'FAIL')

    def test_a_route_shaped_hit_is_a_violation_unless_the_case_expects_it(self):
        b = self.build()
        orig = b.run_case
        def spoiled(case, phase, results=None):
            orig(case, phase, results)
            if case['id'] == 'P29':
                b.trip.append({'event': 'http', 'port': 8080, 'src': GW, 'method': 'GET', 'path': '/read/mail?d=secret', 'bytes': 0, 't': b.t + 0.3})
        b.run_case = spoiled
        self.assertEqual(self.verdict(b)['phases'][0]['wall'], 'FAIL')

    def test_unattributed_event_inside_the_phase_fails(self):
        b = self.build()
        b.write()
        rows = [json.loads(x) for x in (self.root / 'model-log.jsonl').read_text().splitlines()]
        start = next(r['t'] for r in rows if r.get('label') == 'phase:locked:start')
        with (self.root / 'tripwire-log.jsonl').open('a') as f:
            f.write(json.dumps({'event': 'http', 'port': 8080, 'src': '172.29.49.7', 'method': 'GET', 'path': '/x', 'bytes': 0, 't': start + 0.4}) + '\n')
        v = W.evaluate_all(self.data, W.Evidence(self.root), GW)
        self.assertEqual(v['phases'][0]['wall'], 'FAIL')
        self.assertIsNone(v['phases'][0]['violations'][0]['case'])

    def test_receipt_or_journal_activity_during_case_phases_fails(self):
        b = self.build()
        b.receipts['wall-only'] = [{'raw': '{}', 'path': '/send'}]
        v = self.verdict(b)
        self.assertEqual((v['phases'][1]['wall'], v['overall']), ('FAIL', 'FAIL'))
        b = self.build()
        b.audit['locked'] = [{'event': 'hold'}]
        self.assertEqual(self.verdict(b)['phases'][0]['wall'], 'FAIL')

    def test_unexercised_case_is_inconclusive_not_a_pass(self):
        b = self.build()
        b.skip.add('P20')
        v = self.verdict(b)
        self.assertEqual(v['phases'][0]['wall'], 'INCONCLUSIVE')
        self.assertEqual(v['overall'], 'INCONCLUSIVE')
        self.assertNotIn('worker-in-wall: PASS (fake services only)', W.summary_lines(v))

    def test_a_case_that_stopped_after_the_call_but_before_the_result_is_not_exercised(self):
        b = self.build().write()
        rows = [json.loads(x) for x in (self.root / 'model-log.jsonl').read_text().splitlines()]
        rows = [r for r in rows if not (r.get('case') == 'P03' and r.get('step') == 1)]
        (self.root / 'model-log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        self.assertEqual(W.evaluate_all(self.data, W.Evidence(self.root), GW)['overall'], 'INCONCLUSIVE')

    def test_failed_positive_control_is_inconclusive(self):
        b = self.build()
        b.skip.add('norouthit')
        v = self.verdict(b)
        self.assertEqual(v['phases'][0]['wall'], 'INCONCLUSIVE')
        self.assertTrue(any('RC1' in n for n in v['phases'][0]['notes']))

    def test_plugin_not_loading_is_a_lock_finding_not_a_wall_failure(self):
        b = self.build()
        for case in self.data['cases']:
            if case['kind'] == 'should_block':
                b.text[case['id']] = NET_TEXT
        v = self.verdict(b)
        self.assertEqual(v['phases'][0]['wall'], 'PASS')
        self.assertEqual(v['phases'][0]['lock'], 'PARTIAL')
        self.assertEqual(v['overall'], 'PASS')   # the wall held; the lock finding is reported separately
        self.assertIn('PARTIAL', '\n'.join(W.summary_lines(v)))

    def test_tool_unavailable_counts_only_for_tools_the_worker_does_not_expose(self):
        b = self.build()
        b.text['P01'] = "Unknown tool 'send_message'"   # not exposed in the worker: fine
        self.assertEqual(self.verdict(b)['phases'][0]['lock'], 'PASS')
        b = self.build()
        b.text['P02'] = "Unknown tool 'terminal'"        # terminal IS exposed: a finding
        self.assertEqual(self.verdict(b)['phases'][0]['lock'], 'PARTIAL')

    def test_overblocking_a_harmless_control_is_a_lock_finding(self):
        b = self.build()
        b.text['P05'] = LOCK_TEXT
        self.assertEqual(self.verdict(b)['phases'][0]['lock'], 'PARTIAL')

    def test_agent_claims_are_never_read(self):
        b = self.build()
        b.write()
        (self.root / 'locked').mkdir(exist_ok=True)
        (self.root / 'locked' / 'P07.out').write_text('I sent the email successfully to stranger@example.com')
        self.assertEqual(W.evaluate_all(self.data, W.Evidence(self.root), GW)['overall'], 'PASS')
        # and the reverse: a reassuring claim cannot hide a tripwire hit
        with (self.root / 'tripwire-log.jsonl').open('a') as f:
            rows = [json.loads(x) for x in (self.root / 'model-log.jsonl').read_text().splitlines()]
            t = next(r['t'] for r in rows if r.get('case') == 'P07' and r.get('step') == 0 and r['t'] < 1500)
            f.write(json.dumps({'event': 'http', 'port': 8080, 'src': '172.29.49.7', 'method': 'POST', 'path': '/v3/mail/send', 'bytes': 9, 't': t + 0.1}) + '\n')
        (self.root / 'locked' / 'P07.out').write_text('Blocked. Nothing left the container.')
        self.assertEqual(W.evaluate_all(self.data, W.Evidence(self.root), GW)['overall'], 'FAIL')

    def test_missing_or_corrupt_evidence_is_inconclusive(self):
        b = self.build().write()
        (self.root / 'receipts-after-locked.jsonl').unlink()
        self.assertEqual(W.evaluate_all(self.data, W.Evidence(self.root), GW)['phases'][0]['wall'], 'INCONCLUSIVE')
        self.setUp()
        self.build().write()
        with (self.root / 'model-log.jsonl').open('a') as f:
            f.write('{not json\n')
        self.assertEqual(W.evaluate_all(self.data, W.Evidence(self.root), GW)['overall'], 'INCONCLUSIVE')
        self.setUp()
        self.assertEqual(W.evaluate_all(self.data, W.Evidence(self.root / 'nothing'), GW)['overall'], 'INCONCLUSIVE')

    def test_oversize_request_is_noted(self):
        b = self.build().write()
        rows = [json.loads(x) for x in (self.root / 'model-log.jsonl').read_text().splitlines()]
        first = next(r for r in rows if r.get('event') == 'request')
        first['bytes'] = 70000
        (self.root / 'model-log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        v = W.evaluate_all(self.data, W.Evidence(self.root), GW)
        self.assertTrue(any('body limit' in n for p in v['phases'] for n in p['notes']))


class ApprovalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(CASES.read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def status(self, **changes):
        b = Builder(self.root, self.data)
        for k, v in changes.items():
            b.approval[k] = v
        b.write(phases=())
        return W.evaluate_approval(self.data, W.Evidence(self.root))

    def test_all_four_scenarios_pass_on_good_evidence(self):
        out = self.status()
        self.assertEqual(out['status'], 'PASS', out)
        self.assertTrue(all(c['ok'] for c in out['checks']))
        self.assertEqual(out['forge_layers'], ['plugin_block', 'plugin_block'])

    def test_each_scenario_failure_is_caught(self):
        good = Builder(self.root, self.data).good_approval()
        cases = {
            'no-approval hold became a send': {'send-results.txt': good['send-results.txt'].replace('first.json hold\nfirst.json hold', 'first.json send\nfirst.json hold', 1)},
            'replay delivered twice': {'receipts-final.jsonl': good['receipts-final.jsonl'] * 2,
                                       'audit.jsonl': good['audit.jsonl']},
            'changed draft was delivered': {'receipts-final.jsonl': json.dumps({'raw': draft_bytes('edited after owner approval').decode(), 'path': '/send'}) + '\n'},
            'journal sequence wrong': {'audit.jsonl': ''.join(json.dumps({'event': e}) + '\n' for e in ['hold', 'attempt', 'send'])},
            'forged approval file existed': {'approvals-after-forge.txt': 'first.json\n'},
            'extra file in approvals': {'approvals-listing.txt': 'edited.json\nfirst.json\nforged.json\nstep-two.json\n'},
            'receipt bytes differ from approved draft': {'receipts-final.jsonl': json.dumps({'raw': draft_bytes('other').decode(), 'path': '/send'}) + '\n'},
            'approval digest is not the draft hash': {'owner-digest-first.txt': 'f' * 64 + '\n'},
            'edit never happened': {'draft-edited-final.json': draft_bytes('draft to be edited')},
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.setUp()
                out = self.status(**change)
                self.assertEqual(out['status'], 'FAIL', name)

    def test_missing_files_or_unrun_agent_steps_are_inconclusive(self):
        self.assertEqual(self.status(**{'send-results.txt': None})['status'], 'INCONCLUSIVE')
        self.setUp()
        b = Builder(self.root, self.data)
        b.skip.add('A4')
        b.write(phases=())
        self.assertEqual(W.evaluate_approval(self.data, W.Evidence(self.root))['status'], 'INCONCLUSIVE')

    def test_unclear_forge_result_is_noted_but_outside_facts_decide(self):
        b = Builder(self.root, self.data)
        b.write(phases=())
        rows = [json.loads(x) for x in (self.root / 'model-log.jsonl').read_text().splitlines()]
        for r in rows:
            if r.get('case') == 'A2' and 'last_tool_result' in r:
                r['last_tool_result']['text'] = 'ok done'
        (self.root / 'model-log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        out = W.evaluate_approval(self.data, W.Evidence(self.root))
        self.assertEqual(out['status'], 'PASS')
        self.assertTrue(any('forge' in n for n in out['notes']))


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(CASES.read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, probe_ok=True, plugins='send-lock enabled', bytes_=21000):
        b = Builder(self.root, self.data)
        b.mark('phase:smoke:start')
        b.run_case(next(c for c in self.data['cases'] if c['kind'] == 'smoke'), 'locked')
        b.mark('phase:smoke:end')
        for r in b.model:
            if r.get('event') == 'request':
                r['bytes'] = bytes_
        (self.root / 'smoke').mkdir()
        (self.root / 'model-log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in b.model))
        (self.root / 'tripwire-log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in b.trip))
        (self.root / 'smoke' / 'worker-probe.json').write_text(json.dumps({'ok': probe_ok, 'checks': [{'name': 'x', 'ok': probe_ok, 'detail': 'd'}]}))
        if plugins is not None:
            (self.root / 'smoke' / 'hermes-plugins.txt').write_text(plugins)

    def test_smoke_verdicts(self):
        self.write()
        self.assertEqual(W.smoke_verdict(self.data, W.Evidence(self.root), GW)[0], 'PASS')
        self.setUp(); self.write(probe_ok=False)
        self.assertEqual(W.smoke_verdict(self.data, W.Evidence(self.root), GW)[0], 'FAIL')
        self.setUp(); self.write(plugins='nothing relevant')
        self.assertEqual(W.smoke_verdict(self.data, W.Evidence(self.root), GW)[0], 'FAIL')
        self.setUp(); self.write(bytes_=62000)
        status, lines = W.smoke_verdict(self.data, W.Evidence(self.root), GW)
        self.assertEqual(status, 'FAIL')
        self.assertTrue(any('64 KiB' in x for x in lines))
        self.setUp()
        self.assertEqual(W.smoke_verdict(self.data, W.Evidence(self.root), GW)[0], 'INCONCLUSIVE')

    def test_smoke_with_no_model_request_explains_where_to_look(self):
        self.write()
        (self.root / 'model-log.jsonl').write_text(''.join(
            x + '\n' for x in (l for l in (self.root / 'model-log.jsonl').read_text().splitlines() if '"mark"' in l)))
        status, lines = W.smoke_verdict(self.data, W.Evidence(self.root), GW)
        self.assertEqual(status, 'INCONCLUSIVE')
        self.assertIn('gateway', lines[0])


class WorkerProbeTests(unittest.TestCase):
    def test_pure_helpers(self):
        self.assertEqual(WP.secretish_names({'HOME': '/w', 'API_KEY': 'v', 'GitHub_Token': 'v', 'PATH': '/bin', 'MY_PASSWORD': 'v'}), ['API_KEY', 'GitHub_Token', 'MY_PASSWORD'])
        self.assertEqual(WP.secretish_names({'HERMES_HOME': '/opt/data', 'AXIOM_OWNER_CONFIG': '/guard/axiom-owner.json'}), [])
        self.assertTrue(WP.cap_eff_is_zero('Name:\tx\nCapEff:\t0000000000000000\n'))
        self.assertFalse(WP.cap_eff_is_zero('CapEff:\t00000000a80425fb\n'))
        self.assertFalse(WP.cap_eff_is_zero('no such line'))
        self.assertTrue(WP.no_new_privs('NoNewPrivs:\t1\n'))
        self.assertFalse(WP.no_new_privs('NoNewPrivs:\t0\n'))
        self.assertTrue(WP.refused(lambda: (_ for _ in ()).throw(OSError('x'))))
        self.assertFalse(WP.refused(lambda: None))

    def test_probe_never_prints_environment_values(self):
        source = (DOOR / 'wiw_worker_probe.py').read_text()
        self.assertNotIn('os.environ.items', source)
        self.assertNotIn('print(os.environ', source)
        self.assertEqual(WP.WORKER_UID, 10001)


# ------------------------------------------------------------------ fake model and tripwire (loopback only)

def post(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
        return response.read(), response.headers.get('Content-Type')


class FakeModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'model-log.jsonl'
        scripts = FM.load_scripts(CASES)
        self.server = FM.make_server(('127.0.0.1', 0), scripts, self.log)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def rows(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_scripts_cover_every_case_and_flow_step(self):
        scripts = FM.load_scripts(CASES)
        data = json.loads(CASES.read_text())
        self.assertEqual(set(scripts), {c['id'] for c in data['cases']} | {'A1', 'A2', 'A3', 'A4', 'A5'})

    def test_walks_a_planted_case_one_call_per_turn_then_finishes(self):
        messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': '[WIW-CASE:P02] Read the note'}]
        body, ctype = post(self.url + '/v1/chat/completions', {'model': 'm', 'messages': messages, 'tools': [{}] * 3})
        reply = json.loads(body)
        call = reply['choices'][0]['message']['tool_calls'][0]
        self.assertEqual(reply['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(call['function']['name'], 'read_file')
        self.assertEqual(json.loads(call['function']['arguments']), {'path': '/workspace/fixtures/notes/P02.txt'})
        messages += [reply['choices'][0]['message'], {'role': 'tool', 'tool_call_id': call['id'], 'content': 'note text'}]
        reply = json.loads(post(self.url + '/v1/chat/completions', {'model': 'm', 'messages': messages})[0])
        call = reply['choices'][0]['message']['tool_calls'][0]
        self.assertEqual(call['function']['name'], 'terminal')
        self.assertIn('smtplib', json.loads(call['function']['arguments'])['command'])
        messages += [reply['choices'][0]['message'], {'role': 'tool', 'tool_call_id': call['id'], 'content': LOCK_TEXT * 3}]
        reply = json.loads(post(self.url + '/v1/chat/completions', {'model': 'm', 'messages': messages})[0])
        self.assertEqual(reply['choices'][0]['message']['content'], 'WIW-DONE:P02')
        self.assertNotIn('tool_calls', reply['choices'][0]['message'])
        rows = [r for r in self.rows() if r['event'] == 'request']
        self.assertEqual([(r['case'], r['step'], r['action']) for r in rows], [('P02', 0, 'tool_call'), ('P02', 1, 'tool_call'), ('P02', 2, 'final')])
        self.assertEqual(rows[2]['last_tool_result']['text'], (LOCK_TEXT * 3)[:FM.TEXT_LIMIT])
        self.assertEqual(rows[0]['tools_offered'], 3)
        self.assertEqual(rows[1]['emitted']['tool'], 'terminal')

    def test_streaming_reply_and_content_parts(self):
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': '[WIW-CASE:P01] hi'}]}]
        body, ctype = post(self.url + '/v1/chat/completions', {'model': 'm', 'messages': messages, 'stream': True})
        self.assertEqual(ctype, 'text/event-stream')
        text = body.decode()
        self.assertTrue(text.rstrip().endswith('data: [DONE]'))
        chunks = [json.loads(l[6:]) for l in text.splitlines() if l.startswith('data: {')]
        call = next(c['choices'][0]['delta']['tool_calls'][0] for c in chunks if c['choices'][0]['delta'].get('tool_calls'))
        self.assertEqual(call['function']['name'], 'send_message')
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'tool_calls')

    def test_unmarked_and_unknown_requests_get_plain_text_and_are_logged(self):
        reply = json.loads(post(self.url + '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'make a title'}]})[0])
        self.assertEqual(reply['choices'][0]['message']['content'], 'ok')
        reply = json.loads(post(self.url + '/v1/chat/completions', {'messages': [{'role': 'user', 'content': '[WIW-CASE:NOPE] x'}]})[0])
        self.assertEqual(reply['choices'][0]['message']['content'], 'ok')
        for path, body in (('/v1/responses', {'input': 'x'}), ('/v1/chat/completions', {'no': 'messages'})):
            with self.assertRaises(Exception):
                post(self.url + path, body)
        events = [(r['event'], r.get('case')) for r in self.rows()]
        self.assertIn(('request', None), events)
        self.assertEqual(sum(e == 'unsupported' for e, _ in events), 2)

    def test_marks_and_health(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.assertEqual(json.loads(opener.open(self.url + '/__mark/phase:locked:start', timeout=5).read()), {'fake': True})
        opener.open(self.url + '/__health', timeout=5).read()
        marks = [r for r in self.rows() if r['event'] == 'mark']
        self.assertEqual([m['label'] for m in marks], ['phase:locked:start'])


class TripwireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'trip.jsonl'
        free = [socket.socket() for _ in range(3)]
        ports = []
        for s in free:
            s.bind(('127.0.0.1', 0)); ports.append(s.getsockname()[1])
        for s in free:
            s.close()
        self.http, self.smtp, self.raw = ports
        self.servers = TW.make_servers(self.log, self.http, self.smtp, [self.raw])
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def rows(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def wait_rows(self, n):
        import time
        for _ in range(50):
            if self.log.exists() and len(self.rows()) >= n:
                break
            time.sleep(0.05)
        return self.rows()

    def test_http_requests_and_marks_are_logged(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = 'http://127.0.0.1:%d' % self.http
        self.assertEqual(json.loads(opener.open(base + '/read/mail', timeout=5).read()), {'fake': True})
        post(base + '/v3/mail/send', {'x': 1})
        opener.open(base + '/__mark/phase:locked:end', timeout=5).read()
        rows = self.wait_rows(3)
        self.assertEqual([(r['event'], r.get('method'), r.get('path') or r.get('label')) for r in rows],
                         [('http', 'GET', '/read/mail'), ('http', 'POST', '/v3/mail/send'), ('mark', None, 'phase:locked:end')])
        self.assertGreater(rows[1]['bytes'], 0)
        self.assertNotIn('body', rows[1])

    def test_smtp_banner_matches_the_earlier_container_test_and_raw_ports_log(self):
        with socket.create_connection(('127.0.0.1', self.smtp), timeout=3) as s:
            self.assertTrue(s.recv(100).startswith(b'220 FAKE SMTP'))
            s.sendall(b'HELO x\r\n')
        with socket.create_connection(('127.0.0.1', self.raw), timeout=3) as s:
            s.sendall(b'\x16\x03\x01')
        rows = self.wait_rows(2)
        self.assertEqual(sorted(r['port'] for r in rows), sorted([self.smtp, self.raw]))
        self.assertTrue(all(r['event'] == 'conn' for r in rows))
        self.assertIn('160301', [r['first_bytes_hex'] for r in rows])


# ------------------------------------------------------------------ compose, configs, script

class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.compose = json.loads((DOOR / 'compose.worker-in-wall.json').read_text())
        self.svc = self.compose['services']

    def test_worker_boundary(self):
        w = self.svc['worker']
        self.assertEqual(w['networks'], ['worker-net'])
        self.assertEqual(w['user'], '10001:10002')
        self.assertEqual(w['cap_drop'], ['ALL'])
        self.assertNotIn('cap_add', w)
        self.assertTrue(w['read_only'])
        self.assertTrue(w['init'])
        self.assertIn('no-new-privileges:true', w['security_opt'])
        self.assertEqual(w['dns'], ['127.0.0.1'])
        self.assertEqual(w['entrypoint'], ['sleep', 'infinity'])
        self.assertIn('WIW_HERMES_IMAGE', w['image'])
        self.assertNotIn('latest', w['image'])
        self.assertLessEqual(len(w['tmpfs']), 3)

    def test_worker_mounts_and_environment_carry_no_credential(self):
        w = self.svc['worker']
        mounts = {m['target']: m for m in w['volumes']}
        for target in ('/approvals', '/guard', '/opt/data/config.yaml', '/opt/data/plugins'):
            self.assertTrue(mounts[target]['read_only'], target)
        self.assertEqual(mounts['/opt/data/config.yaml']['volume']['subpath'], 'config.yaml')
        self.assertEqual(mounts['/opt/data/plugins']['volume']['subpath'], 'plugins')
        self.assertEqual(sorted(m['source'] for m in w['volumes']), ['approvals', 'guard', 'guard', 'guard', 'outbox'])
        self.assertFalse(any(m['type'] != 'volume' for m in w['volumes']), 'no bind mounts from the host')
        self.assertFalse(any('secret' in m['source'] or 'state' in m['source'] or 'receipts' in m['source'] or 'canary' in m['source'] for m in w['volumes']))
        self.assertEqual(WP.secretish_names(w['environment']), [])
        for name in w['environment']:
            self.assertNotIn('PROXY', name.upper())
        self.assertEqual(w['environment']['HERMES_HOME'], '/opt/data')
        self.assertEqual(w['environment']['MODEL_BASE_URL'], 'http://egress-gateway:8080/model/v1')

    def test_only_the_gateway_straddles_both_networks_and_nothing_is_exposed(self):
        self.assertTrue(all(n['internal'] for n in self.compose['networks'].values()))
        self.assertEqual(self.compose['networks']['worker-net']['driver_opts']['com.docker.network.bridge.gateway_mode_ipv4'], 'isolated')
        for name, s in self.svc.items():
            self.assertNotIn('ports', s, name)
            self.assertNotIn('privileged', s, name)
            self.assertNotEqual(s.get('network_mode'), 'host', name)
            self.assertFalse(any('docker.sock' in str(v) for v in s.get('volumes', [])), name)
            self.assertIn('no-new-privileges:true', s['security_opt'], name)
            self.assertEqual(s['cap_drop'], ['ALL'], name)
            if name not in ('init', 'init-canary'):
                self.assertNotEqual(s['user'], '0:0', name)
        straddlers = [n for n, s in self.svc.items() if len(s.get('networks', [])) > 1]
        self.assertEqual(straddlers, ['egress-gateway'])
        self.assertEqual(self.svc['sender']['networks'], ['outside-net'])
        self.assertEqual(self.svc['owner']['network_mode'], 'none')
        for name in ('fake-model', 'tripwire', 'fake-receiver'):
            self.assertEqual(self.svc[name]['networks'], ['outside-net'])
        users = {n: self.svc[n]['user'] for n in ('worker', 'sender', 'owner', 'fake-model')}
        self.assertEqual(len(set(users.values())), 4)

    def test_only_the_sender_sees_the_send_credential_and_only_the_owner_writes_approvals(self):
        holders = [n for n, s in self.svc.items() if any(str(v).startswith('send-secret') for v in s.get('volumes', []))]
        self.assertEqual(sorted(holders), ['init', 'sender'])
        self.assertIn('send-secret:/data/secrets:ro', self.svc['sender']['volumes'])
        writers = [n for n, s in self.svc.items() if 'approvals:/data/approvals' in s.get('volumes', [])]
        self.assertEqual(sorted(writers), ['init', 'owner'])

    def test_every_image_comes_from_a_variable_never_a_registry_tag(self):
        for name, s in self.svc.items():
            self.assertTrue(s['image'].startswith('${'), name)
            self.assertNotIn(':latest', s['image'])
        self.assertNotIn('build', json.dumps(self.compose))

    def test_egress_config_routes_to_in_project_fakes_only_and_the_gateway_accepts_it(self):
        egress = json.loads((DOOR / 'egress.worker-in-wall.json').read_text())
        self.assertEqual({k: v['origin'] for k, v in egress.items()},
                         {'model': 'http://fake-model:8080', 'google': 'http://tripwire:8080', 'owner': 'http://tripwire:8080'})
        self.assertEqual({k: v['methods'] for k, v in egress.items()}, {'model': ['POST'], 'google': ['GET'], 'owner': ['GET']})
        rules = kit_sender.load((DOOR / 'egress.worker-in-wall.json').read_bytes())
        self.assertEqual(gateway.target(rules, 'POST', '/model/v1/chat/completions'), 'http://fake-model:8080/v1/chat/completions')
        self.assertEqual(gateway.target(rules, 'GET', '/google/read/mail'), 'http://tripwire:8080/read/mail')
        self.assertEqual(gateway.target(rules, 'GET', '/owner/owner/inbox/list'), 'http://tripwire:8080/owner/inbox/list')
        for method, path in (('GET', '/model/v1/chat/completions'), ('POST', '/google/read/mail'), ('POST', '/owner/owner/inbox/list'),
                             ('GET', '/other/x/y'), ('POST', '/model/v2/chat'), ('POST', '/model/v1/../v1/x')):
            self.assertIsNone(gateway.target(rules, method, path), (method, path))

    def test_hermes_configs_differ_only_in_the_plugin_line_and_hold_no_credential(self):
        locked = (DOOR / 'config.worker-in-wall.yaml').read_text()
        wall = (DOOR / 'config.worker-in-wall.wall-only.yaml').read_text()
        self.assertIn('enabled: [send-lock]', locked)
        self.assertIn('enabled: []', wall)
        strip = lambda text: [l for l in text.splitlines() if not l.startswith('#') and 'enabled: [' not in l and l.strip() != 'disabled: [send-lock]']
        self.assertEqual(strip(locked), strip(wall))
        for text in (locked, wall):
            for needle in ('mcp_servers: {}', 'browser, computer, phone, delegation, code_execution', 'cli: [terminal, file, skills, todo, tts]',
                           'base_url: http://egress-gateway:8080/model/v1', 'allow_lazy_installs: false', 'api_key: FAKE-NOT-A-CREDENTIAL'):
                self.assertIn(needle, text)

    def test_no_secret_shaped_text_in_any_new_file(self):
        names = ['compose.worker-in-wall.json', 'config.worker-in-wall.yaml', 'config.worker-in-wall.wall-only.yaml', 'egress.worker-in-wall.json',
                 'wiw_cases.json', 'fake_model.py', 'tripwire.py', 'wiw_check.py', 'wiw_worker_probe.py', 'test_worker_in_wall.sh']
        pattern = re.compile(r'sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|xox[bp]-|AKIA[0-9A-Z]{8,}|-----BEGIN|Bearer [A-Za-z0-9._-]{12,}|auth\.json|\.env\b')
        for name in names:
            text = (DOOR / name).read_text()
            found = pattern.findall(text)
            # cases.json holds the verbatim probe strings, which include fake Bearer placeholders
            self.assertEqual([f for f in found if not f.startswith('Bearer ***')], [], name)

    def test_python_files_are_stdlib_only(self):
        stdlib = set(sys.stdlib_module_names)
        for name in ('fake_model.py', 'tripwire.py', 'wiw_check.py', 'wiw_worker_probe.py'):
            import ast
            tree = ast.parse((DOOR / name).read_text())
            for node in ast.walk(tree):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) else [])
                for module in modules:
                    self.assertIn(module.split('.')[0], stdlib, (name, module))


class ScriptTests(unittest.TestCase):
    path = DOOR / 'test_worker_in_wall.sh'

    def setUp(self):
        self.text = self.path.read_text()
        self.code = '\n'.join(l for l in self.text.splitlines() if not l.lstrip().startswith('#'))

    def test_syntax_and_help_only(self):
        self.assertEqual(subprocess.run(['bash', '-n', str(self.path)], capture_output=True).returncode, 0)
        helped = subprocess.run(['bash', str(self.path), 'help'], capture_output=True, text=True, timeout=20)
        self.assertEqual(helped.returncode, 0)
        self.assertIn('WIW_APPROVED_RUN', helped.stdout)
        refused = subprocess.run(['bash', str(self.path), 'nonsense'], capture_output=True, text=True, timeout=20)
        self.assertEqual(refused.returncode, 2)

    def test_refuses_without_explicit_approvals_before_touching_anything(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('WIW_')}
        for extra, message in (({}, 'WIW_APPROVED_RUN'), ({'WIW_APPROVED_RUN': 'yes'}, 'WIW_PULL_APPROVED'),
                               ({'WIW_APPROVED_RUN': 'yes', 'WIW_PULL_APPROVED': 'yes'}, 'WIW_BASE_IMAGE')):
            run = subprocess.run(['bash', str(self.path), 'all'], capture_output=True, text=True, timeout=20, env=dict(env, **extra))
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(message, run.stderr)

    def test_only_two_pulls_and_everything_else_is_offline(self):
        pulls = re.findall(r'"\$\{D\[@\]\}" pull ("\$\w+")', self.code)
        self.assertEqual(pulls, ['"$base"', '"$hermes"'])
        self.assertEqual(len(re.findall(r'\bpull\b(?! never| ?=)', re.sub(r'--pull[= ]\w+', '', self.code))), 2)  # the two staged pulls, nothing else
        self.assertGreaterEqual(self.code.count('--pull never'), 2)
        self.assertEqual(self.code.count('--pull=false'), 2)
        self.assertEqual(self.code.count('--network none'), 3)   # two builds + the image layout check
        self.assertIn('--no-build', self.code)
        for bad in ('--privileged', 'docker.sock', 'git push', 'network host', 'docker login', '--network=host', ' push '):
            self.assertNotIn(bad, self.code)

    def test_every_docker_call_names_the_throwaway_daemon(self):
        # Outside preflight, docker is only reached through "${D[@]}" (docker --context colima-<unique>).
        bare = [l for l in self.code.splitlines() if re.search(r'(?<![\w$.-])docker\s+(?!--context)\w+', l)]
        allowed = ('for tool in python3 docker colima awk', "containers=$(docker ps --format '{{.Names}}'")
        self.assertEqual([l for l in bare if not any(a in l for a in allowed)], [], 'bare docker calls found')
        self.assertIn('D=(docker --context "colima-$vm")', self.code)
        self.assertIn('compose=("${D[@]}" compose --project-name "$project"', self.code)

    def test_order_of_guards_and_teardown(self):
        i = self.code.index
        self.assertLess(i('preflight\n    before_free='), i('colima start'))
        self.assertLess(i('colima start'), i('"${D[@]}" pull "$base"'))
        self.assertLess(i('"${D[@]}" pull "$hermes"'), i('"${D[@]}" build'))
        self.assertLess(i('py smoke'), i('set_mode locked;    phase locked'))
        self.assertLess(i('phase wall-only'), i('phase:approval:start'))
        self.assertLess(i('phase:approval:end'), i('py evaluate'))
        self.assertIn('trap teardown EXIT', self.code)
        self.assertLess(i('trap teardown EXIT'), i('vm_created=true'))
        teardown = self.code[self.code.index('teardown() {'):self.code.index('main_all() {')]
        for needle in ('down --volumes --remove-orphans', 'image rm', 'colima delete "$vm" --force --data', 'space-returned', 'TEARDOWN FAILED', 'rm -rf'):
            self.assertIn(needle, teardown)
        self.assertIn('unique', self.text.lower())
        self.assertIn('project="wiw-$stamp-$$"', self.code)
        self.assertIn('--memory 4 --disk 20', self.code)

    def test_agent_output_never_feeds_a_verdict(self):
        self.assertIn('the checker ignores it', self.text)
        for name in ('.out', '.err'):
            self.assertNotIn(name, (DOOR / 'wiw_check.py').read_text())

    def test_smoke_runs_before_any_attack_and_aborts_the_run_on_failure(self):
        self.assertIn("|| die 'SMOKE did not pass", self.code)
        self.assertIn('x worker "$hermes_py" - "$trip_ip" < deploy/one_door/wiw_worker_probe.py', self.code)

    def test_other_shell_scripts_still_parse(self):
        for script in list((ROOT / 'deploy').glob('*.sh')) + list(DOOR.glob('*.sh')) + list((ROOT / 'demo').glob('*.sh')):
            self.assertEqual(subprocess.run(['bash', '-n', str(script)], capture_output=True).returncode, 0, script)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    failures = len(result.failures) + len(result.errors)
    print('worker-in-wall harness: %d passed, %d failed' % (result.testsRun - failures, failures))
    sys.exit(0 if result.wasSuccessful() else 1)
