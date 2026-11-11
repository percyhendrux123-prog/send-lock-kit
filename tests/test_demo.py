#!/usr/bin/env python3
"""Offline demo acceptance checks. Docker execution is a separate host acceptance."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def test_fixture_sources_span_three_days_and_fake_contacts_only(self):
        fixture = ROOT / 'demo/fixtures'
        fields = {'mail': 'last_message_at', 'calendar': 'start', 'tasks': 'created_at'}
        for name, field in fields.items():
            records = json.loads((fixture / (name + '.json')).read_text())
            self.assertTrue({'2026-11-08', '2026-11-09', '2026-11-10'}.issubset({r[field][:10] for r in records}), name)
        for person in json.loads((fixture / 'people.json').read_text()):
            self.assertTrue(person['email'].endswith('@example.com'))
            self.assertRegex(person['phone'], r'^\+1-\d{3}-555-01\d{2}$')
        all_text = '\n'.join(p.read_text() for p in fixture.glob('*.json'))
        self.assertTrue(all(email.endswith('@example.com') for email in re.findall(r'[\w.+-]+@[\w.-]+', all_text)))

    def test_northside_cli_is_real_and_slipping_items_present(self):
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/rundown.py'), '--fixtures',
                              str(ROOT / 'demo/fixtures'), '--now', '2026-11-10T08:00:00-06:00'],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Northside Studio — 2026-11-10', run.stdout)
        self.assertIn("What's slipping:", run.stdout)
        self.assertIn('Replace two worn studio mats — 2 days overdue', run.stdout)
        self.assertIn('Morgan Vale: intro session question — 2 days', run.stdout)
        self.assertIn('Check the studio speaker cable — stalled 2 days', run.stdout)
        self.assertNotIn('Cancelled evening class', run.stdout)
        self.assertNotIn('Archive completed room tidy', run.stdout)

    def test_container_and_wrapper_are_inert_and_shells_parse(self):
        dockerfile = (ROOT / 'demo/Dockerfile').read_text()
        self.assertIn('FROM ubuntu:24.04', dockerfile)
        self.assertIn('install_vps.sh --non-interactive --container', dockerfile)
        self.assertIn('USER hermes', dockerfile)
        self.assertIn('demo/verify.sh', dockerfile)
        wrapper = (ROOT / 'demo/run.sh').read_text()
        self.assertIn('--network none', wrapper)
        self.assertIn('OUTPUT.txt', wrapper)
        self.assertIn('colima stop', wrapper)
        self.assertIn('pipefail', wrapper)
        verifier = (ROOT / 'demo/verify.sh').read_text()
        self.assertIn('tests/test_*.py', verifier)
        self.assertIn('--fixtures', verifier)
        for path in (ROOT / 'deploy').glob('*.sh'):
            self.assertEqual(subprocess.run(['bash', '-n', str(path)], capture_output=True).returncode, 0)
        for path in (ROOT / 'demo').glob('*.sh'):
            self.assertEqual(subprocess.run(['bash', '-n', str(path)], capture_output=True).returncode, 0)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    failures = len(result.failures) + len(result.errors)
    print('Northside demo: %d passed, %d failed' % (result.testsRun - failures, failures))
    sys.exit(0 if result.wasSuccessful() else 1)
