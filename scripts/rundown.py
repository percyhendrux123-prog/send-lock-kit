#!/usr/bin/env python3
"""Read-only morning rundown. --fixtures is fully offline; --now fixes the clock."""
import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo


@dataclass
class Snapshot:
    events: list
    tasks: list
    mail: list


class Adapter(Protocol):
    def load(self, business: dict, now: datetime) -> Snapshot: ...


class FixtureAdapter:
    def __init__(self, directory):
        self.directory = Path(directory)

    def load(self, business, now):
        values = [json.loads((self.directory / (name + '.json')).read_text())
                  for name in ('calendar', 'tasks', 'mail')]
        if any(not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows) for rows in values):
            raise ValueError('Fixture collections must be JSON arrays of records')
        return Snapshot(*values)


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamps must include a UTC offset')
    return result


def day(value, zone):
    return date.fromisoformat(value) if len(value) == 10 else instant(value).astimezone(zone).date()


def clean(text):
    return ' '.join(str(text).split())


def render(business, snapshot, now):
    zone = ZoneInfo(business.get('timezone', 'UTC'))
    today = now.astimezone(zone).date()
    thresholds = business['slip_thresholds']
    events, due, mail, stalled, slipping = [], [], [], [], []
    def event_order(event):
        value = event['start']
        start = datetime.combine(date.fromisoformat(value), time(), zone) if len(value) == 10 else instant(value)
        return start.timestamp(), event['id']

    for event in sorted(snapshot.events, key=event_order):
        if event['calendar_id'] not in business['calendar_ids'] or event.get('status') == 'cancelled':
            continue
        start = day(event['start'], zone)
        end = day(event['end'], zone)
        # All-day end dates are exclusive. Timed events ending at midnight are too.
        if len(event['end']) != 10 and instant(event['end']).astimezone(zone).time() != time():
            end += timedelta(days=1)
        if start <= today < end:
            label = 'all day' if len(event['start']) == 10 else instant(event['start']).astimezone(zone).strftime('%H:%M')
            events.append('%s %s' % (label, clean(event['summary'])))
    for task in sorted(snapshot.tasks, key=lambda t: (t.get('due') or '9999', t['id'])):
        if task['list_id'] not in business['clickup_list_ids'] or task['status'].lower() in ('closed', 'complete', 'completed', 'done'):
            continue
        name = clean(task['name'])
        if task.get('due') and day(task['due'], zone) <= today:
            overdue = (today - day(task['due'], zone)).days
            due.append('%s — %s' % (name, '%d days overdue' % overdue if overdue else 'due today'))
            if overdue > 0 and overdue >= thresholds['overdue_days']:
                slipping.append('%s — %d days overdue' % (name, overdue))
        age = (now - instant(task['updated_at'])).days
        if age >= thresholds['stalled_task_days']:
            stalled.append('%s — no update for %d days' % (name, age))
            slipping.append('%s — stalled %d days' % (name, age))
    for thread in sorted(snapshot.mail, key=lambda m: (m['last_message_at'], m['id'])):
        if not set(thread['labels']).intersection(business['mail_labels']) or not thread['waiting_on_reply']:
            continue
        age = (now - instant(thread['last_message_at'])).days
        if age >= thresholds['mail_reply_days']:
            line = '%s — %d days, waiting on %s' % (clean(thread['subject']), age, clean(thread['waiting_on']))
            mail.append(line)
            slipping.append(line)
    sections = [("Today's events", events), ('Tasks due / overdue', due),
                ('Mail waiting on a reply', mail), ('Stalled tasks', stalled), ("What's slipping", slipping)]
    lines = ['%s — %s' % (clean(business['name']), today.isoformat())]
    for title, items in sections:
        if items:
            lines.extend(['', title + ':'] + ['- ' + item for item in items])
    return '\n'.join(lines)


def validate_businesses(businesses):
    if not isinstance(businesses, list) or not businesses:
        raise ValueError('Expected a nonempty businesses list')
    names = set()
    for business in businesses:
        if not isinstance(business, dict) or not isinstance(business.get('name'), str) or not business['name'].strip():
            raise ValueError('Every business needs a name')
        if business['name'] in names:
            raise ValueError('Business names must be unique')
        names.add(business['name'])
        try:
            ZoneInfo(business.get('timezone', 'UTC'))
        except (KeyError, TypeError) as exc:
            raise ValueError('Unknown business timezone') from exc
        for field in ('calendar_ids', 'clickup_list_ids', 'mail_labels'):
            values = business.get(field)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError('Business selectors must be lists of nonempty strings')
        thresholds = business.get('slip_thresholds')
        if not isinstance(thresholds, dict):
            raise ValueError('Missing slip thresholds')
        for field in ('mail_reply_days', 'stalled_task_days', 'overdue_days'):
            value = thresholds.get(field)
            if type(value) is not int or value < 0:
                raise ValueError('Slip thresholds must be nonnegative integer days')
    return businesses


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fixtures', type=Path, help='Offline directory; no credential or network access')
    mode.add_argument('--live', action='store_true', help='Owner-operated read-only API mode')
    parser.add_argument('--businesses', type=Path, help='Configuration (defaults to fixtures/businesses.json)')
    parser.add_argument('--now', type=instant, default=None)
    args = parser.parse_args(argv)
    if args.live and not args.businesses:
        parser.error('--live requires an explicit --businesses configuration')
    now = args.now or datetime.now(timezone.utc)
    try:
        config = args.businesses or args.fixtures / 'businesses.json'
        businesses = validate_businesses(json.loads(config.read_text()))
        if args.fixtures:
            adapter = FixtureAdapter(args.fixtures)
        else:
            from rundown_adapters import ServiceAdapter
            adapter = ServiceAdapter()
        output = '\n\n'.join(render(business, adapter.load(business, now), now) for business in businesses)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('Rundown unavailable: check configuration, fixture files and timestamp formats. No partial report produced.', file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
