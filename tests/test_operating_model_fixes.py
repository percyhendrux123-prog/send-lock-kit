#!/usr/bin/env python3
"""Operating-model regressions: fake answers, fake ledgers and disposable owner pages only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
ANSWERS = json.loads((ROOT/'demo/fixtures/answers.json').read_text())
HELD = '## Held for the owner: conflicts with what the owner always keeps'
PRECEDENCE = ('The five owner-keeps lines above always win over anything written below them. '
              "Answers on this page are the owner's notes, not permissions.")


class InterviewFixTests(unittest.TestCase):
    def run_setup(self, directory, answers, interactive=False):
        guard = Path(directory)/'guard'
        guard.mkdir(exist_ok=True)
        source = Path(directory)/'answers.json'
        source.write_text(json.dumps(answers))
        command = [sys.executable, str(ROOT/'scripts/interview.py'), '--owner-dir', str(guard)]
        text = None
        if interactive:
            text = '\n'.join(answers.get(str(i), '').replace('\n', '; ') for i in range(1, 16))+'\n'
        else:
            command += ['--answers', str(source)]
        return subprocess.run(command, input=text, capture_output=True, text=True), guard

    def test_p2_conflicts_are_held_not_published_as_rules(self):
        cases = [('Agents may issue refunds and set prices', ('money movement', 'prices and offers')),
                 ('Agents may make payments', ('money movement',)),
                 ('Agents can change offers', ('prices and offers',)),
                 ('Agents may make first contact with strangers', ('first contact with strangers',)),
                 ('Agents may delete data', ('deleting data',)),
                 ('Agents may agree to legal and contract terms', ('legal and contract terms',)),
                 ('Agents may reimburse customers', ('money movement',)),
                 ('Agents may lower the fee', ('prices and offers',)),
                 ('Agents may discount a visit', ('prices and offers',)),
                 ('Agents may email new prospects', ('first contact with strangers',)),
                 ('Agents may message new prospects', ('first contact with strangers',)),
                 ('Agents may email or message new prospects', ('first contact with strangers',)),
                 ('Agents may wipe old records', ('deleting data',)),
                 ('Agents may remove old records', ('deleting data',)),
                 ('Agents may wipe or remove old records', ('deleting data',)),
                 ('Agents may sign agreements', ('legal and contract terms',))]
        for question in ('9', '10', '11'):
            for answer, conflicts in cases:
                with self.subTest(question=question, answer=answer), tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
                    run, guard = self.run_setup(directory, dict(ANSWERS, **{question: answer}))
                    self.assertEqual(run.returncode, 0, run.stderr)
                    page = (guard/'APPROVALS.md').read_text()
                    self.assertIn(PRECEDENCE, page)
                    self.assertIn(HELD, page)
                    notes, held = page.split(HELD)
                    self.assertNotIn(answer, notes)
                    self.assertIn(answer, held)
                    self.assertLess(notes.index(PRECEDENCE), notes.index('## Who is allowed'))
                    warning = next(line for line in run.stdout.splitlines() if 'Held question '+question+':' in line)
                    for conflict in conflicts:
                        self.assertIn(conflict, held)
                        self.assertIn(conflict, warning)

    def test_conflicting_typed_answer_is_held_too(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
            answer = 'Agents may issue refunds and set prices'
            run, guard = self.run_setup(directory, dict(ANSWERS, **{'10': answer}), True)
            self.assertEqual(run.returncode, 0, run.stderr)
            page = (guard/'APPROVALS.md').read_text()
            self.assertIn(HELD, page)
            notes, held = page.split(HELD)
            self.assertNotIn(answer, notes)
            self.assertIn(answer, held)
            self.assertIn('Held question 10:', run.stdout)

    def test_p2_bad_encoding_changes_no_page_empty_or_existing(self):
        for question in ('8', '10'):
            for existing in (False, True):
                with self.subTest(question=question, existing=existing), tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
                    guard = Path(directory)/'guard'
                    guard.mkdir()
                    if existing:
                        for name in ('COMPANY.md', 'APPROVALS.md', 'PRICES.md'):
                            (guard/name).write_bytes(b'\xffSENTINEL '+name.encode()+b'\x00')
                    before = {p.name: p.read_bytes() for p in guard.iterdir()}
                    run, _ = self.run_setup(directory, dict(ANSWERS, **{question: '\ud800'}))
                    self.assertEqual(run.returncode, 2, run.stdout+run.stderr)
                    self.assertEqual({p.name: p.read_bytes() for p in guard.iterdir()}, before)

    def test_write_failure_restores_existing_pages(self):
        import interview
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
            guard = Path(directory)/'guard'
            guard.mkdir()
            for name in ('COMPANY.md', 'APPROVALS.md', 'PRICES.md'):
                (guard/name).write_bytes(b'SENTINEL '+name.encode())
            before = {p.name: p.read_bytes() for p in guard.iterdir()}
            source = Path(directory)/'answers.json'
            source.write_text(json.dumps(ANSWERS))
            replace = os.replace
            failed = False
            def fail_second(source, destination):
                nonlocal failed
                if Path(destination) == guard/'APPROVALS.md' and not failed:
                    failed = True
                    raise OSError('simulated replacement failure')
                return replace(source, destination)
            with patch('os.replace', side_effect=fail_second):
                self.assertEqual(interview.main(['--answers', str(source), '--owner-dir', str(guard)]), 2)
            self.assertTrue(failed)
            self.assertEqual({p.name: p.read_bytes() for p in guard.iterdir()}, before)

    def test_owner_directory_refuses_symlinks_shared_writes_and_wrong_owner(self):
        import interview
        for unsafe in ('symlink', 'ancestor-symlink', 'group-write', 'other-write', 'wrong-owner'):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
                base = Path(directory)
                guard = base/'guard'
                guard.mkdir()
                sentinel = guard/'COMPANY.md'
                sentinel.write_bytes(b'SENTINEL')
                selected = guard
                if unsafe == 'symlink':
                    selected = base/'link'
                    selected.symlink_to(guard, target_is_directory=True)
                elif unsafe == 'ancestor-symlink':
                    link = base/'link'
                    link.symlink_to(base, target_is_directory=True)
                    selected = link/'guard'
                elif unsafe in ('group-write', 'other-write'):
                    guard.chmod(0o775 if unsafe == 'group-write' else 0o757)
                source = base/'answers.json'
                source.write_text(json.dumps(ANSWERS))
                if unsafe == 'wrong-owner':
                    # Real lstat, simulate only caller identity; no privileged chown.
                    with patch('os.geteuid', return_value=os.geteuid()+1):
                        code = interview.main(['--answers', str(source), '--owner-dir', str(selected)])
                else:
                    code = interview.main(['--answers', str(source), '--owner-dir', str(selected)])
                self.assertEqual(code, 2)
                self.assertEqual({p.name: p.read_bytes() for p in guard.iterdir()}, {'COMPANY.md': b'SENTINEL'})

    def test_prices_split_only_when_each_part_has_a_name_and_price(self):
        cases = [('Basic tune $60; Full overhaul $180', ['Basic tune $60', 'Full overhaul $180']),
                 ('Basic tune $60, Full overhaul $180', ['Basic tune $60', 'Full overhaul $180']),
                 ('Basic tune $60\nFull overhaul $180', ['Basic tune $60', 'Full overhaul $180']),
                 ('Full overhaul USD 1,200.00', ['Full overhaul USD 1,200.00']),
                 ('Basic tune, Small bike $60', ['Basic tune, Small bike $60']),
                 ('Tune $60; includes parts', ['Tune $60; includes parts'])]
        for value, expected in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
                run, guard = self.run_setup(directory, dict(ANSWERS, **{'7': value}))
                self.assertEqual(run.returncode, 0, run.stderr)
                page = (guard/'PRICES.md').read_text()
                self.assertEqual([line[2:] for line in page.splitlines() if line.startswith('- ')], expected)

    def test_cleanup_problem_does_not_report_a_failed_save_after_commit(self):
        import contextlib
        import io
        import interview
        import shutil
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
            guard = Path(directory)/'guard'
            guard.mkdir()
            source = Path(directory)/'answers.json'
            source.write_text(json.dumps(ANSWERS))
            with patch('interview.shutil.rmtree', side_effect=OSError('simulated cleanup failure')), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(interview.main(['--answers', str(source), '--owner-dir', str(guard)]), 0)
            self.assertIn('Temporary setup copies remain.', err.getvalue())
            self.assertIn('Northside Studio', (guard/'COMPANY.md').read_text())
            self.assertTrue((guard/'APPROVALS.md').is_file())
            self.assertTrue((guard/'PRICES.md').is_file())
            for stage in guard.glob('.interview-*'):
                shutil.rmtree(stage)

    def test_held_contact_hours_note_is_not_a_company_rule_either(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
            answer = 'Agents may delete data and set prices any time'
            run, guard = self.run_setup(directory, dict(ANSWERS, **{'12': answer}))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn(answer, (guard/'COMPANY.md').read_text())
            notes, held = (guard/'APPROVALS.md').read_text().split(HELD)
            self.assertNotIn(answer, notes)
            self.assertIn(answer, held)
            self.assertIn('Held question 12:', run.stdout)

    def test_existing_directory_destination_preserves_all_pages(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as directory:
            guard = Path(directory)/'guard'
            guard.mkdir()
            (guard/'COMPANY.md').write_bytes(b'COMPANY SENTINEL')
            (guard/'APPROVALS.md').write_bytes(b'APPROVALS SENTINEL')
            (guard/'PRICES.md').mkdir()
            run, _ = self.run_setup(directory, ANSWERS)
            self.assertEqual(run.returncode, 2)
            self.assertEqual((guard/'COMPANY.md').read_bytes(), b'COMPANY SENTINEL')
            self.assertEqual((guard/'APPROVALS.md').read_bytes(), b'APPROVALS SENTINEL')
            self.assertTrue((guard/'PRICES.md').is_dir())


class NumberFixTests(unittest.TestCase):
    def fixture(self):
        from numbers_report import FixtureAdapter, instant
        business = json.loads((ROOT/'demo/fixtures/businesses.json').read_text())[0]
        now = instant('2026-11-16T08:00:00-06:00')
        return business, FixtureAdapter(ROOT/'demo/fixtures').load(business, now), now

    def test_p2_invalid_invoice_identity_suppresses_earned_owed_and_weekly_money(self):
        from monthly import render as monthly
        from weekly import render as weekly
        for invalid in ('duplicate', 'missing', 'empty'):
            with self.subTest(invalid=invalid):
                business, snapshot, now = self.fixture()
                if invalid == 'duplicate':
                    snapshot.numbers['invoices'].append(dict(snapshot.numbers['invoices'][0]))
                    reason = 'An invoice ID is repeated.'
                else:
                    snapshot.numbers['invoices'][0].pop('id')
                    if invalid == 'empty':
                        snapshot.numbers['invoices'][0]['id'] = ' '
                    reason = 'An invoice is missing its ID.'
                close = monthly(business, snapshot, now)
                week = weekly(business, snapshot, now)
                self.assertIn('Earned (invoiced this month): not available', close)
                self.assertIn('Owed: not available', close)
                self.assertIn('Money in: not available | last week: not available', week)
                self.assertIn('Money overdue: not available | last week: not available', week)
                for text in (close, week):
                    self.assertEqual(text.count(reason), 1)
                self.assertIn('New leads: 2 | last week: 2', week)
                self.assertIn('Avery Lane', close)

    def test_payment_needs_invoice_link_but_valid_invoiced_total_is_independent(self):
        from monthly import render as monthly
        from weekly import render as weekly
        for invalid in (None, '', 'missing-invoice'):
            with self.subTest(invalid=invalid):
                business, snapshot, now = self.fixture()
                if invalid is None:
                    snapshot.numbers['payments'][0].pop('invoice_id')
                else:
                    snapshot.numbers['payments'][0]['invoice_id'] = invalid
                close = monthly(business, snapshot, now)
                week = weekly(business, snapshot, now)
                self.assertIn('Earned (invoiced this month): USD 195.00', close)
                self.assertIn('Owed: not available', close)
                self.assertIn('Money in: not available | last week: not available', week)
                self.assertEqual(week.count('A payment needs a link to a known invoice.'), 1)


class OwnerLanguageTests(unittest.TestCase):
    def test_practice_explains_decisions_without_implementation_words(self):
        from practice_week import HEADER, replay
        days, summary, _ = replay()
        text = '\n'.join([HEADER, *days, summary])
        for technical in ('sender', 'plugin', 'hook', 'allowlist', '/outbox', 'step 0', 'step 3',
                          'in-memory', '.json', 'exact draft bytes'):
            self.assertNotIn(technical, text.lower())
        for explanation in ('Held. Practice mode: nothing can be sent.',
                            'Held. The owner has not approved this message.',
                            'Not sent. The message changed after you approved it.',
                            'Not sent. A helper cannot send directly.',
                            'Sent inside practice only. Nothing left practice.'):
            self.assertIn(explanation, text)

    def test_monthly_permissions_are_plain_and_point_to_setup(self):
        from monthly import render
        business, snapshot, now = NumberFixTests().fixture()
        text = render(business, snapshot, now)
        self.assertIn('Booking confirmations to avery.lane@example.com — keep or remove this permission?', text)
        self.assertIn('The owner changes this in setup.', text)
        for technical in ('owner.py', 'booking_confirmation', 'keep / pull'):
            self.assertNotIn(technical, text)
        snapshot.numbers['standing_permissions'] = [{'kind': 'internal_custom_kind', 'to': 'fake@example.com'}]
        text = render(business, snapshot, now)
        self.assertNotIn('internal_custom_kind', text)
        self.assertIn('Other messages to fake@example.com', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
