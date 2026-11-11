#!/usr/bin/env python3
"""Replay the actual sender and plugin in-process; forbid any socket access."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))

class PracticeTests(unittest.TestCase):
    def test_week_is_deterministic_and_runs_real_guard_paths_without_network(self):
        from practice_week import HEADER, replay
        import sender
        import owner
        original = sender.process
        original_config = sender.owner_config
        original_approve = owner.approve
        calls, approvals, configs = [], [], []
        def inspect(root, filename, deliver):
            step = json.loads((root/'guard/axiom-owner.json').read_text())['step']
            result = original(root, filename, deliver)
            reason = json.loads((root/'state/audit.jsonl').read_text().splitlines()[-1])['reason']
            calls.append((step, filename, result, reason))
            return result
        def inspect_config(path):
            if path.parent.parent.name == 'practice':
                configs.append(path.read_bytes())
            return original_config(path)
        def inspect_approval(root, filename, digest):
            approvals.append((filename, (root/'outbox'/filename).read_bytes()))
            return original_approve(root, filename, digest)
        before = dict(os.environ)
        with patch('socket.socket', side_effect=AssertionError('No network in practice')), \
                patch('sender.http_delivery', side_effect=AssertionError('No HTTP delivery')), \
                patch('sender.process', side_effect=inspect), \
                patch('sender.owner_config', side_effect=inspect_config), \
                patch('owner.approve', side_effect=inspect_approval):
            days, summary, receipts = replay()
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(len(days), 7)
        self.assertEqual(calls, [
            (0, 'practice.json', 'hold', 'practice; sender disabled'),
            (3, 'intro.json', 'hold', 'no approval'),
            (3, 'intro.json', 'send', 'receiver acknowledged'),
            (3, 'rowan.json', 'hold', 'no approval'),
            (3, 'edited.json', 'refuse', 'approval does not match exact draft bytes'),
            (3, 'unapproved.json', 'hold', 'no approval'),
        ])
        self.assertEqual([name for name, _ in approvals], ['intro.json', 'edited.json'])
        self.assertEqual([json.loads(raw) for _, raw in approvals], [
            {'kind': 'reply', 'to': 'morgan.vale@example.com', 'body': 'Your intro visit is booked for Thursday at 10.'}
        ]*2)
        self.assertGreaterEqual(len(configs), 2)  # Initial sender read and final read.
        self.assertTrue(all(raw == configs[0] for raw in configs))
        self.assertEqual(json.loads(configs[-1])['step'], 0)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0], {'kind': 'reply', 'to': 'morgan.vale@example.com', 'body': 'Your intro visit is booked for Thursday at 10.'})
        text = '\n\n'.join([HEADER]+days+[summary])
        for expected in ('New lead: Morgan Vale', 'New lead: Rowan Ellis', 'waiting on owner',
                         'Invoice nov12 crossed the 3-day unpaid limit', 'Held. The owner has not approved this message.',
                         'Sent inside practice only. Nothing left practice.', 'Not sent. The message changed after you approved it.',
                         'Not sent. A helper cannot send directly.', 'Held. Practice mode: nothing can be sent.',
                         'Approved:', 'Held:', 'Refused:'):
            self.assertIn(expected, text)
        self.assertEqual(replay(), (days, summary, receipts))
        self.assertEqual(sum("Morning rundown" in d for d in days), 7)
        self.assertIn('separate pretend mailbox', text)
        self.assertEqual([next(line for line in d.splitlines() if line.startswith('Invoices overdue:')) for d in days],
                         ['Invoices overdue: USD '+value for value in ('120.00', '120.00', '0.00', '0.00', '0.00', '0.00', '50.00')])

    def test_selected_day_replays_only_the_prefix_and_matches_all(self):
        from practice_week import replay, main
        all_days, _, _ = replay()
        for day in range(1, 8):
            days, summary, receipts = replay(day)
            self.assertEqual(days, all_days[:day])
            self.assertEqual(len(receipts), int(day >= 2))
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(main(['--day', str(day)]), 0)
            self.assertIn(all_days[day-1], out.getvalue())
            if day > 1:
                self.assertNotIn('Day 1 —', out.getvalue())
        for args in (['--day', '0'], ['--day', '8'], ['--all', '--day', '2'], []):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)

    def test_archived_demos_match_fresh_real_cli_runs(self):
        cases = [('practice_week.py', ['--all'], 'PRACTICE-WEEK.txt'),
                 ('weekly.py', ['--fixtures', str(ROOT/'demo/fixtures'), '--now', '2026-11-16T08:00:00-06:00'], 'WEEKLY.txt'),
                 ('monthly.py', ['--fixtures', str(ROOT/'demo/fixtures'), '--now', '2026-11-16T08:00:00-06:00'], 'MONTHLY.txt')]
        for script, args, output in cases:
            with self.subTest(script=script):
                run = subprocess.run([sys.executable, str(ROOT/'scripts'/script), *args], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout, (ROOT/'demo'/output).read_text())

    def test_cli_output_is_repeatable_from_another_directory(self):
        cmd = [sys.executable, str(ROOT/'scripts/practice_week.py'), '--all']
        runs = [subprocess.run(cmd, cwd='/tmp', capture_output=True, text=True) for _ in range(2)]
        self.assertEqual(runs[0].returncode, 0, runs[0].stderr)
        self.assertEqual(runs[0].stdout, runs[1].stdout)

if __name__ == '__main__':
    unittest.main(verbosity=2)
