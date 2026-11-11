#!/usr/bin/env python3
"""Owner-run setup. Use the existing owner guard directory, never a worker folder."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy/one_door'))
from owner import atomic
from install import check_node, check_root, sync_directory

QUESTIONS = (
    'What is the business called?',
    'In one sentence, what do you sell?',
    'Who buys it?',
    'How does a new customer usually find you?',
    'What is the first thing that happens after someone asks about buying?',
    'What does a finished job look like?',
    'List each thing you sell and its price. (required; at least one)',
    'Are there terms a customer must agree to before you start? If yes, where is that written?',
    'Who is allowed to say yes to a new customer? (default: only you)',
    'What may be sent without asking you first? (default: nothing)',
    'What must never be sent or promised, whatever happens?',
    'What hours may customers be contacted?',
    'How long may a customer wait for a reply before it counts as slipping? (days)',
    'How long may an invoice go unpaid before it counts as overdue? (days)',
    'What are the three numbers you would check first on a Monday? (optional; default: new leads, money in, money overdue)',
)
REQUIRED = {1: 'The pages need the name of the business.',
            2: 'The desks need to know what you sell.',
            3: 'The desks need to know who you serve.',
            7: 'A price sheet needs at least one thing you sell and its price.',
            13: 'The rundown needs a reply limit in whole days.',
            14: 'The numbers need an unpaid invoice limit in whole days.'}
OWNER_KEEPS = ('money movement', 'prices and offers', 'first contact with strangers',
               'deleting data', 'legal and contract terms')
DEFAULTS = {9: 'only you', 10: 'nothing', 15: 'new leads, money in, money overdue'}


def conflicts(value):
    # Best-effort, conservative keyword matching, not a permission interpreter.
    # It may also hold a safeguard mentioning these subjects. The explicit
    # owner-keeps override on the page is the real guarantee, not this detector.
    patterns = (r'\b(refund\w*|reimburs\w*|pay\w*|money|transfer\w*|charg\w*|purchas\w*)\b',
                r'\b(pric\w*|fee\w*|offer\w*|discount\w*)\b',
                r'\b(stranger\w*|prospect\w*|cold\s+(contact\w*|outreach|call\w*|email\w*)|first\s+contact)\b',
                r'\b(delet\w*|eras\w*|purge\w*|wip\w*|remov\w*\s+(?:old\s+)?records?)\b',
                r'\b(legal|contract\w*|agreement\w*|terms)\b')
    return [name for name, pattern in zip(OWNER_KEEPS, patterns) if re.search(pattern, value, re.IGNORECASE)]


def unknown(value):
    return value.lower().replace('’', "'").strip(' .!?') in ('', "don't know", "i don't know", 'unknown', 'not sure')


def plain(value):
    # An answer is text, never a new Markdown row, heading or instruction section.
    return ' '.join(value.split()).replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;')


def priced_item(line):
    prices = re.finditer(r'(?:\b(?:USD|EUR|GBP)\s*|[$£€]\s*)?\d+(?:\.\d{1,2})?|\bfree\b', line, re.IGNORECASE)
    return any(re.search(r'[A-Za-z]', line[:price.start()]) for price in prices)


def price_items(value):
    items = []
    for line in value.splitlines():
        if not line.strip():
            continue
        parts = re.split(r';\s*|,\s+(?=[A-Z])', line)
        # Split only if every proposed item has its own name and price. Never
        # split numeric commas or rewrite the owner's amounts.
        items.extend(parts if all(priced_item(part) for part in parts) else [line])
    return items


def pages(answers):
    a = {k: plain(v) for k, v in answers.items()}
    company = ['# Company', '', a[1], '']
    for i in (2, 3, 4, 5, 6, 12, 13, 14, 15):
        note = 'Held for the owner; see Approvals. This is not a rule.' if i == 12 and conflicts(answers[i]) else a[i]
        company += ['## '+QUESTIONS[i-1], note, '']
    approvals = ['# Approvals', '',
                 'These pages do not grant a standing permission. Messages still need the required OK.',
                 'The owner changes permissions in setup, not with an interview answer.', '',
                 '| Desk | Does alone | Drafts for OK | Never |', '|---|---|---|---|']
    for desk, alone, drafts in (
        ('Chief of staff', 'Read the work. Prepare the rundown and numbers.', 'Plans and reminders.'),
        ('Front desk', 'Read incoming questions. Track new leads.', 'Replies and booking notes.'),
        ('Client care', 'Read client questions. Flag late replies and renewals.', 'Client replies and renewal notes.'),
    ):
        approvals.append('| '+ ' | '.join((desk, alone, drafts, 'Send without the required OK. Change owner-kept decisions.'))+' |')
    approvals += ['', '## The owner always keeps', *('- '+v for v in OWNER_KEEPS), '',
                  'The five owner-keeps lines above always win over anything written below them. '
                  "Answers on this page are the owner's notes, not permissions.", '']
    held = []
    for i in (9, 10, 11, 12):
        found = conflicts(answers[i])
        approvals += ['## '+QUESTIONS[i-1],
                      'Held for the owner; see the section below. This is not a rule.' if found else a[i], '']
        if found:
            held += [f'### Question {i}: '+QUESTIONS[i-1],
                     'Conflicts with: '+', '.join(found)+'.', 'Owner note, not approved: '+a[i], '']
    if held:
        approvals += ['## Held for the owner: conflicts with what the owner always keeps', '', *held]
    prices = ['# Prices', '', '## Things sold and prices',
              *('- '+plain(line) for line in price_items(answers[7])), '',
              '## Terms', a[8], '', 'Only the owner sets or changes prices, offers and terms.', '']
    return {'COMPANY.md': '\n'.join(company), 'APPROVALS.md': '\n'.join(approvals), 'PRICES.md': '\n'.join(prices)}


def write_pages(directory, payloads):
    # Finish preparation (including destination checks and backups) before any
    # page replacement. Reuse the owner writer for the staged files.
    directory = directory.absolute()
    check_root(directory, rehearsal=True)  # Existing helper rejects symlink ancestors.
    check_node(directory, directory=True)  # Caller-owned; no shared write access.
    originals = {}
    for name in payloads:
        path = directory/name
        if path.exists() or path.is_symlink():
            check_node(path)
        originals[name] = path.read_bytes() if path.exists() else None
    stage = Path(tempfile.mkdtemp(prefix='.interview-', dir=directory))
    changed = []
    cleanup = True
    try:
        for name, raw in payloads.items():
            atomic(stage/name, raw)
            if originals[name] is not None:
                atomic(stage/('before-'+name), originals[name])
        try:
            for name in payloads:
                changed.append(name)
                os.replace(stage/name, directory/name)
            sync_directory(directory)
        except OSError:
            # Retain recovery copies if the storage also refuses rollback.
            cleanup = False
            failures = []
            for name in reversed(changed):
                try:
                    if originals[name] is None:
                        (directory/name).unlink(missing_ok=True)
                    else:
                        os.replace(stage/('before-'+name), directory/name)
                except OSError:
                    failures.append(name)
            try:
                sync_directory(directory)
            except OSError:
                failures.append('owner directory sync')
            cleanup = not failures
            if failures:
                print('Setup rollback failed for: '+', '.join(failures)+'. '
                      'Recovery copies remain in '+str(stage)+'. Check all three pages before use.', file=sys.stderr)
            raise
    finally:
        if cleanup:
            try:
                shutil.rmtree(stage)
            except OSError:
                # Saving has already succeeded (or an earlier error is being
                # propagated). Cleanup must not falsely report a failed save.
                print('Temporary setup copies remain. Ask the owner to check the setup folder.', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--answers', type=Path, help='JSON object keyed by question number, 1 to 15. Values are text.')
    parser.add_argument('--owner-dir', type=Path, default=Path('/data/guard'),
                        help='Existing owner-owned guard directory. Worker must mount it read-only.')
    args = parser.parse_args(argv)
    try:
        supplied = json.loads(args.answers.read_text()) if args.answers else None
        if args.answers and (not isinstance(supplied, dict) or
                any(k not in {str(i) for i in range(1, 16)} or not isinstance(v, str) for k, v in supplied.items())):
            raise ValueError('Answers must be text keyed by question number.')
        answers = {}
        for i, question in enumerate(QUESTIONS, 1):
            if supplied is None:
                try:
                    value = input(f'{i}. {question}\n> ').strip()
                except EOFError:
                    value = ''
            else:
                value = supplied.get(str(i), '').strip()
            invalid = unknown(value)
            if i == 7 and not invalid:
                # Keep prices as owner text; require an item name before a
                # price. Accept e.g. "Intro: USD 25" or "Intro $25".
                invalid = not all(priced_item(line) for line in value.splitlines() if line.strip())
            if i in (13, 14) and not invalid:
                invalid = not value.isascii() or not value.isdecimal() or len(value) > 6
            if i in REQUIRED and invalid:
                print(f'Stopped at question {i}: {question}\n{REQUIRED[i]}\nNothing was written.')
                return 3
            answers[i] = DEFAULTS.get(i, 'Not set. Ask the owner before acting.') if invalid else value
        for i in (9, 10, 11, 12):
            found = conflicts(answers[i])
            if found:
                print(f'Held question {i}: conflicts with '+', '.join(found)+'. The owner keeps these decisions.')
        # Same owner-side writer and existing read-only guard mount. No chmod,
        # chown, mount, approval or owner configuration changes are made here.
        payloads = {name: text.encode('utf-8') for name, text in pages(answers).items()}
        write_pages(args.owner_dir, payloads)
    except (OSError, ValueError, UnicodeError):
        print('Setup could not finish. Check the answers file and owner directory. Ask the owner to check the three pages before use.', file=sys.stderr)
        return 2
    print('Wrote COMPANY.md, APPROVALS.md and PRICES.md in the owner directory.')
    print('The worker reads these pages. Only the owner changes them. No permission was granted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
