#!/usr/bin/env python3
"""Rundown tests: synthetic records, no account access or external HTTP."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
NOW = '2026-11-10T08:00:00-06:00'
BUSINESS = {'name': 'Northside Studio', 'timezone': 'America/Chicago',
            'calendar_ids': ['studio'], 'clickup_list_ids': ['10'], 'mail_labels': ['Northside'],
            'slip_thresholds': {'mail_reply_days': 2, 'stalled_task_days': 2, 'overdue_days': 1}}


class RundownTests(unittest.TestCase):
    def test_fixture_cli_renders_rundown_and_slipping_deterministically(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            fixture = Path(directory)
            records = {
                'businesses': [BUSINESS],
                'calendar': [{'id': 'event', 'calendar_id': 'studio', 'summary': 'Morning mobility',
                              'start': '2026-11-10T09:00:00-06:00', 'end': '2026-11-10T10:00:00-06:00'}],
                'tasks': [{'id': 'task', 'list_id': '10', 'name': 'Replace worn mats', 'status': 'open',
                           'due': '2026-11-09', 'updated_at': '2026-11-08T08:00:00-06:00'}],
                'mail': [{'id': 'thread', 'labels': ['Northside'], 'subject': 'Intro session question',
                          'last_message_at': '2026-11-08T08:00:00-06:00', 'waiting_on_reply': True,
                          'waiting_on': 'owner'}],
            }
            for name, value in records.items():
                (fixture / (name + '.json')).write_text(json.dumps(value))
            command = [sys.executable, str(ROOT / 'scripts/rundown.py'), '--fixtures', directory, '--now', NOW]
            runs = [subprocess.run(command, capture_output=True, text=True) for _ in range(2)]
        self.assertEqual(runs[0].returncode, 0, runs[0].stderr)
        self.assertEqual(runs[0].stdout, runs[1].stdout)
        for text in ('Northside Studio', "Today's events", 'Morning mobility', 'Tasks due / overdue',
                     'Replace worn mats', 'Mail waiting on a reply', 'Intro session question',
                     'Stalled tasks', "What's slipping", 'overdue', '2 days'):
            self.assertIn(text, runs[0].stdout)


    def test_business_separation_timezone_boundaries_and_empty_sections(self):
        from rundown import Snapshot, instant, render
        snapshot = Snapshot([
            {'id': 'today', 'calendar_id': 'studio', 'summary': 'Late UTC',
             'start': '2026-11-11T01:00:00Z', 'end': '2026-11-11T02:00:00Z'},
            {'id': 'all', 'calendar_id': 'studio', 'summary': 'All day',
             'start': '2026-11-10', 'end': '2026-11-11'},
            {'id': 'ended', 'calendar_id': 'studio', 'summary': 'Already ended',
             'start': '2026-11-09', 'end': '2026-11-10'},
            {'id': 'other', 'calendar_id': 'private', 'summary': 'Other business',
             'start': '2026-11-10', 'end': '2026-11-11'},
            {'id': 'cancel', 'calendar_id': 'studio', 'summary': 'Cancelled class', 'status': 'cancelled',
             'start': '2026-11-10', 'end': '2026-11-11'}], [], [])
        result = render(BUSINESS, snapshot, instant(NOW))
        for good in ('19:00 Late UTC', 'all day All day'):
            self.assertIn(good, result)
        for absent in ('Already ended', 'Other business', 'Cancelled class', 'Tasks due', 'Mail waiting', "What's slipping"):
            self.assertNotIn(absent, result)

    def test_mail_stall_thresholds_closed_future_and_other_business(self):
        from rundown import Snapshot, instant, render
        tasks = [dict(id='open', list_id='10', name='Due today', status='open', due='2026-11-10', updated_at=NOW),
                 dict(id='closed', list_id='10', name='Closed task', status='closed', due='2026-11-08', updated_at='2026-11-08T00:00:00Z'),
                 dict(id='other', list_id='99', name='Private task', status='open', due='2026-11-08', updated_at='2026-11-08T00:00:00Z')]
        mails = [dict(id='waiting', labels=['Northside'], subject='Waiting exactly two days', waiting_on_reply=True,
                      waiting_on='owner', last_message_at='2026-11-08T08:00:00-06:00'),
                 dict(id='fresh', labels=['Northside'], subject='Fresh thread', waiting_on_reply=True,
                      waiting_on='owner', last_message_at='2026-11-08T08:00:01-06:00'),
                 dict(id='answered', labels=['Northside'], subject='Resolved thread', waiting_on_reply=False,
                      waiting_on='owner', last_message_at='2026-11-08T08:00:00-06:00'),
                 dict(id='private', labels=['Private'], subject='Private mail', waiting_on_reply=True,
                      waiting_on='owner', last_message_at='2026-11-08T08:00:00-06:00'),
                 dict(id='future', labels=['Northside'], subject='Future mail', waiting_on_reply=True,
                      waiting_on='owner', last_message_at='2026-11-11T08:00:00-06:00')]
        result = render(BUSINESS, Snapshot([], tasks, mails), instant(NOW))
        self.assertIn('Due today — due today', result)
        self.assertIn('Waiting exactly two days', result)
        for absent in ('Closed task', 'Private task', 'Fresh thread', 'Resolved thread', 'Private mail', 'Future mail', 'Stalled tasks'):
            self.assertNotIn(absent, result)

    def test_configuration_validation_and_naive_clock_rejected(self):
        from rundown import validate_businesses, instant
        import copy
        self.assertEqual(validate_businesses([BUSINESS]), [BUSINESS])
        for field, value in [('timezone', 'Missing/Zone'), ('calendar_ids', 'all'), ('mail_labels', [1]),
                             ('slip_thresholds', {'mail_reply_days': -1}), ('name', '')]:
            cfg = copy.deepcopy(BUSINESS)
            cfg[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_businesses([cfg])
        with self.assertRaises(ValueError):
            validate_businesses([BUSINESS, BUSINESS])
        with self.assertRaises(ValueError):
            instant('2026-11-10T08:00:00')

    def test_fixture_missing_or_invalid_fails_without_partial_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            folder = Path(directory)
            (folder / 'businesses.json').write_text(json.dumps([BUSINESS]))
            command = [sys.executable, str(ROOT / 'scripts/rundown.py'), '--fixtures', directory, '--now', NOW]
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stdout, '')
            self.assertNotIn('Traceback', run.stderr)
            for name in ('calendar', 'tasks', 'mail'):
                (folder / (name + '.json')).write_text('{}')
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stdout, '')


    def test_live_cli_uses_service_adapter_without_network(self):
        import io
        from unittest.mock import Mock, patch
        from rundown import Snapshot, main
        adapter = Mock()
        adapter.load.return_value = Snapshot([], [], [])
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            config = Path(directory) / 'businesses.json'
            config.write_text(json.dumps([BUSINESS]))
            with patch('rundown_adapters.ServiceAdapter', return_value=adapter), patch('sys.stdout', io.StringIO()) as out:
                self.assertEqual(main(['--live', '--businesses', str(config), '--now', NOW]), 0)
            self.assertEqual(out.getvalue(), 'Northside Studio — 2026-11-10\n')
        adapter.load.assert_called_once()


    def test_mixed_offsets_sort_by_actual_start_not_text(self):
        from rundown import Snapshot, instant, render
        events = [dict(id='later', calendar_id='studio', summary='Later class',
                       start='2026-11-10T09:00:00-06:00', end='2026-11-10T10:00:00-06:00'),
                  dict(id='earlier', calendar_id='studio', summary='Earlier class',
                       start='2026-11-10T10:00:00Z', end='2026-11-10T11:00:00Z')]
        result = render(BUSINESS, Snapshot(events, [], []), instant(NOW))
        self.assertLess(result.index('04:00 Earlier class'), result.index('09:00 Later class'))


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    failures = len(result.failures) + len(result.errors)
    print('rundown: %d passed, %d failed' % (result.testsRun - failures, failures))
    sys.exit(0 if result.wasSuccessful() else 1)
