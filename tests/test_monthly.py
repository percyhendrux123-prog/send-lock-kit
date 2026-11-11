#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'deploy/one_door'))
from rundown import Snapshot, instant
NOW = instant('2026-11-16T08:00:00-06:00')
BUSINESS = json.loads((ROOT/'demo/fixtures/businesses.json').read_text())[0]

class MonthlyTests(unittest.TestCase):
    def test_missing_data_never_claims_no_renewals_or_permissions(self):
        from monthly import render
        text = render(BUSINESS, Snapshot([], [], []), NOW)
        self.assertEqual(text.count('not available'), 4)
        self.assertNotIn('USD 0', text)

    def test_month_to_date_owed_next_thirty_days_and_print_only_permissions(self):
        from monthly import render
        from numbers_report import FixtureAdapter
        snapshot = FixtureAdapter(ROOT/'demo/fixtures').load(BUSINESS, NOW)
        before = repr(snapshot)
        guard = ROOT/'demo/owner/guard'
        config_before = {p.name: p.read_bytes() for p in guard.iterdir()}
        with patch('builtins.input', side_effect=AssertionError('Must not ask for input')), \
                patch('owner.atomic', side_effect=AssertionError('Review must never write owner settings')):
            text = render(BUSINESS, snapshot, NOW)
        self.assertEqual(repr(snapshot), before)
        self.assertEqual({p.name: p.read_bytes() for p in guard.iterdir()}, config_before)
        for expected in ('Earned (invoiced this month): USD 195.00', 'Owed: USD 50.00',
                         'Avery Lane — 2026-11-20', 'Booking confirmations', 'avery.lane@example.com',
                         'keep or remove this permission?', 'The owner changes this in setup.'):
            self.assertIn(expected, text)
        self.assertNotIn('Casey Park', text)
        self.assertLessEqual(len(text.splitlines()), 16)

    def test_renewal_boundaries_missing_fields_and_known_empty(self):
        from monthly import render
        from numbers_report import NumbersSnapshot
        snapshot = NumbersSnapshot([], [], [], {'invoices': [], 'payments': [], 'renewals': [], 'standing_permissions': []})
        text = render(BUSINESS, snapshot, NOW)
        self.assertIn('Owed: USD 0.00', text)
        self.assertIn('Renewals due in the next 30 days: none', text)
        snapshot.numbers['renewals'] = [{'client': 'Today', 'due': '2026-11-16'}, {'client': 'Edge', 'due': '2026-12-16'}]
        text = render(BUSINESS, snapshot, NOW)
        self.assertIn('Today', text)
        self.assertNotIn('Edge', text)
        snapshot.numbers['renewals'].append({'client': 'Missing'})
        self.assertIn('Renewals due in the next 30 days: not available', render(BUSINESS, snapshot, NOW))
        snapshot.numbers['standing_permissions'] = [{'kind': 'broken'}]
        self.assertIn('Standing permissions: not available', render(BUSINESS, snapshot, NOW))

if __name__ == '__main__':
    unittest.main(verbosity=2)
