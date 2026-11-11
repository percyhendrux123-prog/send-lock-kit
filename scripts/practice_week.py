#!/usr/bin/env python3
"""Step 0 practice for Northside Studio. No accounts, sockets or model calls."""
import argparse
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

from numbers_report import FixtureAdapter, available, balances, money
from rundown import Snapshot, instant, render as rundown

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT/'demo/fixtures'
sys.path.insert(0, str(ROOT/'deploy/one_door'))
import owner
import sender

START = instant('2026-11-09T08:00:00-06:00')
INTRO = {'kind': 'reply', 'to': 'morgan.vale@example.com',
         'body': 'Your intro visit is booked for Thursday at 10.'}
ROWAN = {'kind': 'reply', 'to': 'rowan.ellis@example.com',
         'body': 'The owner is reviewing your class visit question.'}
HEADER = ('Practice mode. Nothing connected. All names and money are fake.\n'
          'Nothing can be sent outside practice. Later lessons use a separate pretend mailbox\n'
          'to show what happens after approval. No owner setting is changed.')
MESSAGE_NAMES = {'practice.json': 'practice message', 'intro.json': "Morgan's intro reply",
                 'rowan.json': "Rowan's reply", 'edited.json': 'changed intro reply',
                 'unapproved.json': 'unapproved reply', 'direct worker send': 'direct send attempt'}


def box(path, step):
    for name in ('guard', 'outbox', 'approvals', 'state', 'secrets'):
        (path/name).mkdir(parents=True)
    cfg = {'step': step, 'owner_name': 'Demo Owner', 'standing_permissions': [], 'allow_imessage': False}
    owner.atomic(path/'guard/axiom-owner.json', json.dumps(cfg).encode())
    (path/'secrets/send-token').write_text('FAKE-DEMO-ONLY')
    return path


def replay(until=7):
    if type(until) is not int or not 1 <= until <= 7:
        raise ValueError('Choose day 1 through 7')
    business = json.loads((FIXTURES/'businesses.json').read_text())[0]
    schedule = json.loads((FIXTURES/'practice_week.json').read_text())
    spec = importlib.util.spec_from_file_location('practice_send_lock', ROOT/'plugins/send-lock/__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    days, receipts, approved, held, refused = [], [], [], [], []
    with tempfile.TemporaryDirectory(prefix='northside-practice-') as directory:
        root = Path(directory)
        practice = box(root/'practice', 0)
        lesson = box(root/'fake-send-lesson', 3)

        def deliver(raw, token):
            if token != 'FAKE-DEMO-ONLY':
                raise ValueError('Only the fake receiver is allowed')
            receipts.append(json.loads(raw))

        def draft(name, value):
            (lesson/'outbox'/name).write_text(json.dumps(value))

        def approve(name):
            digest = hashlib.sha256((lesson/'outbox'/name).read_bytes()).hexdigest()
            owner.approve(lesson, name, digest)
            approved.append(name)

        def process(path, name, lines):
            event = sender.process(path, name, deliver)
            row = json.loads((path/'state/audit.jsonl').read_text().splitlines()[-1])
            if event != row['event']:
                raise ValueError('Sender result did not match its log')
            explanations = {
                ('hold', 'practice; sender disabled'): 'Held. Practice mode: nothing can be sent.',
                ('hold', 'no approval'): 'Held. The owner has not approved this message.',
                ('refuse', 'approval does not match exact draft bytes'): 'Not sent. The message changed after you approved it.',
                ('send', 'receiver acknowledged'): 'Sent inside practice only. Nothing left practice.',
            }
            lines.append(explanations.get((event, row['reason']), 'Not sent. Ask the owner to check this message.'))
            if event == 'hold':
                held.append(name)
            elif event == 'refuse':
                refused.append(name)

        for index, script in enumerate(schedule[:until], 1):
            now = START+timedelta(days=index-1)
            snapshot = FixtureAdapter(FIXTURES).load(business, now)
            tasks = [t for t in snapshot.tasks if instant(t['created_at']) <= now]
            mail = [m.copy() for m in snapshot.mail if instant(m['last_message_at']) <= now]
            if index >= 3:
                for thread in mail:
                    if thread['id'] == 'question-08':
                        thread['waiting_on_reply'] = False
            # Arrival belongs to the previous day's events, then appears in the next morning's rundown.
            for lead in snapshot.numbers['leads']:
                if START <= instant(lead['at']) < now:
                    mail.append({'id': lead['name'], 'labels': business['mail_labels'],
                                 'subject': lead['name']+': class visit question',
                                 'last_message_at': lead['at'], 'waiting_on_reply': not (
                                     lead['name'] == 'Morgan Vale' and index >= 3), 'waiting_on': 'owner'})
            lines = [f'Day {index} — {now.date().isoformat()}', '', 'Morning rundown',
                     rundown(business, Snapshot(snapshot.events, tasks, mail), now),
                     'Invoices overdue: '+money(available(lambda: balances(snapshot, business, now, True)), business),
                     '', 'Events arriving today:', *('- '+e for e in script['events']),
                     '', 'Owner decision: '+script['decide']]
            for action in script['actions']:
                if action == 'practice_hold':
                    process(practice, 'practice.json', lines)
                elif action == 'draft_intro':
                    draft('intro.json', INTRO)
                    lines += ['Draft for morgan.vale@example.com: '+INTRO['body']]
                    process(lesson, 'intro.json', lines)
                elif action == 'approve_intro':
                    approve('intro.json')
                    process(lesson, 'intro.json', lines)
                elif action == 'hold_rowan':
                    draft('rowan.json', ROWAN)
                    lines += ['Draft for rowan.ellis@example.com: '+ROWAN['body']]
                    process(lesson, 'rowan.json', lines)
                elif action == 'edit_after_approval':
                    draft('edited.json', INTRO)
                    lines += ['Owner approves the original reply: '+INTRO['body']]
                    approve('edited.json')
                    edited = dict(INTRO, body='Your intro visit is now Friday at 11.')
                    draft('edited.json', edited)
                    lines += ['Changed after approval: '+edited['body']]
                    process(lesson, 'edited.json', lines)
                elif action == 'worker_send':
                    # Invoke the real hook. Its attempted command is never executed.
                    old = {k: os.environ.get(k) for k in ('HERMES_HOME', 'AXIOM_OWNER_CONFIG')}
                    try:
                        os.environ['HERMES_HOME'] = str(practice/'guard')
                        os.environ['AXIOM_OWNER_CONFIG'] = str(practice/'guard/axiom-owner.json')
                        result = plugin.pre_tool_call(tool_name='send_message', args={'to': 'rowan.ellis@example.com', 'text': 'Unapproved reply'})
                    finally:
                        for key, value in old.items():
                            if value is None:
                                os.environ.pop(key, None)
                            else:
                                os.environ[key] = value
                    if not result or result['action'] != 'block':
                        raise ValueError('Practice worker send was not blocked')
                    lines += ['Not sent. A helper cannot send directly. Prepare a draft for the owner to review.']
                    refused.append('direct worker send')
                    draft('unapproved.json', ROWAN)
                    process(lesson, 'unapproved.json', lines)
                else:
                    raise ValueError('Unknown practice action')
            days.append('\n'.join(lines))
        if sender.owner_config(practice/'guard/axiom-owner.json')['step'] != 0:
            raise ValueError('Practice must stay at step 0')
    summary = '\n'.join([
        f'Practice summary through day {until}',
        'Approved: '+(', '.join(MESSAGE_NAMES[name] for name in approved) or 'none')+'.',
        'Sent inside practice: '+str(len(receipts))+'. Nothing was sent outside practice.',
        'Held: '+(', '.join(MESSAGE_NAMES[name] for name in sorted(set(held))) or 'none')+'. Held means it did not leave at that point.',
        'Refused: '+(', '.join(MESSAGE_NAMES[name] for name in refused) or 'none')+'.',
        'Practice mode is still on. No owner setting or standing permission changed.',
    ])
    return days, summary, receipts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--day', type=int, choices=range(1, 8))
    mode.add_argument('--all', action='store_true')
    args = parser.parse_args(argv)
    try:
        days, summary, _ = replay(args.day or 7)
    except (OSError, ValueError, KeyError, TypeError):
        print('Practice could not finish. Check the practice examples. Nothing was connected.', file=sys.stderr)
        return 2
    print('\n\n'.join([HEADER, *(days if args.all else days[-1:]), summary]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
