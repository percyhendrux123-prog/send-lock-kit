#!/usr/bin/env python3
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from rundown import Snapshot, instant
NOW = instant('2026-11-16T08:00:00-06:00')
BUSINESS = json.loads((ROOT/'demo/fixtures/businesses.json').read_text())[0]

class WeeklyTests(unittest.TestCase):
    def test_missing_histories_are_not_zero(self):
        from weekly import render
        text = render(BUSINESS, Snapshot([], [], []), NOW)
        self.assertEqual(text.count('not available'), 10)
        self.assertNotIn('USD 0', text)

    def test_fixture_counts_and_previous_week_are_business_scoped(self):
        from numbers_report import FixtureAdapter
        from weekly import render
        business = dict(BUSINESS, invoice_overdue_days=3)
        text = render(business, FixtureAdapter(ROOT/'demo/fixtures').load(business, NOW), NOW)
        for expected in ('New leads: 2 | last week: 2', 'Replies sent: 1 | last week: 1',
                         'Money in: USD 120.00 | last week: USD 25.00',
                         'Money overdue: USD 50.00 | last week: USD 120.00',
                         'Tasks late: 4 | last week: 1'):
            self.assertIn(expected, text)
        self.assertLessEqual(len(text.splitlines()), 12)

    def test_boundary_future_partial_payments_and_missing_fields(self):
        from weekly import render
        from numbers_report import FixtureAdapter
        business = dict(BUSINESS, invoice_overdue_days=3)
        snapshot = FixtureAdapter(ROOT/'demo/fixtures').load(business, NOW)
        snapshot.numbers['leads'].append({'at': NOW.isoformat()})  # right endpoint excluded
        snapshot.numbers['payments'].append({'invoice_id': 'nov12', 'at': '2026-11-15T12:00:00-06:00', 'amount': '20.01', 'currency': 'USD'})
        text = render(business, snapshot, NOW)
        self.assertIn('New leads: 2 | last week: 2', text)
        self.assertIn('Money overdue: USD 29.99', text)
        del snapshot.numbers['leads'][0]['at']
        self.assertIn('New leads: not available | last week: not available', render(business, snapshot, NOW))
        snapshot.numbers['payments'][0]['currency'] = 'EUR'
        self.assertIn('Money in: not available', render(business, snapshot, NOW))

    def test_live_adapter_contract_and_cli_clock(self):
        import io
        from unittest.mock import patch
        from weekly import main
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'businesses.json'
            config.write_text(json.dumps([BUSINESS]))
            with patch('rundown_adapters.ServiceAdapter') as factory, patch('sys.stdout', new_callable=io.StringIO) as out:
                factory.return_value.load.return_value = Snapshot([], [], [])
                self.assertEqual(main(['--live', '--businesses', str(config), '--now', NOW.isoformat()]), 0)
                self.assertIn('New leads: not available', out.getvalue())
                factory.return_value.load.assert_called_once_with(BUSINESS, NOW)
        run = subprocess.run([sys.executable, str(ROOT/'scripts/weekly.py'), '--fixtures', str(ROOT/'demo/fixtures'), '--now', '2026-11-16'], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, '')

if __name__ == '__main__':
    unittest.main(verbosity=2)
