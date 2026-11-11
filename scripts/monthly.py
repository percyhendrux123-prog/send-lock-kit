#!/usr/bin/env python3
"""Month-to-date close. Permission review prints only; it changes nothing."""
from datetime import timedelta
import sys
from zoneinfo import ZoneInfo
from numbers_report import available, balances, clean, day, flow, money, money_issue, rows, run_cli


def renewals(snapshot, zone, today):
    found = []
    for row in rows(snapshot, 'renewals'):
        due = day(row['due'], zone)
        client = row['client']
        if not isinstance(client, str) or not client.strip():
            raise ValueError('Missing client')
        if today <= due < today+timedelta(days=30):
            found.append((due.isoformat(), clean(client)))
    return [f'{client} — {due}' for due, client in sorted(found)]


def permissions(snapshot):
    found = []
    for row in rows(snapshot, 'standing_permissions'):
        if set(row) != {'kind', 'to'} or any(not isinstance(v, str) or not v.strip() for v in row.values()):
            raise ValueError('Missing permission')
        label = {'booking_confirmation': 'Booking confirmations', 'reply': 'Replies',
                 'reminder': 'Reminders'}.get(row['kind'], 'Other messages')
        found.append(f"{label} to {clean(row['to'])} — keep or remove this permission?")
    return sorted(found)


def render(business, snapshot, now):
    zone = ZoneInfo(business.get('timezone', 'UTC'))
    local = now.astimezone(zone)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    earned = available(lambda: flow(snapshot, 'invoices', start, local, business))
    owed = available(lambda: balances(snapshot, business, now))
    due = available(lambda: renewals(snapshot, zone, local.date()))
    standing = available(lambda: permissions(snapshot))
    lines = [f"{clean(business['name'])} — monthly close through {local.date().isoformat()}",
             f'Earned (invoiced this month): {money(earned, business)}', f'Owed: {money(owed, business)}']
    issue = money_issue(snapshot)
    if issue:
        lines.append(issue)
    for title, values in (('Renewals due in the next 30 days', due), ('Standing permissions', standing)):
        if values:
            lines += [title+':', *('- '+v for v in values)]
        else:
            lines += [title+': '+('not available' if values is None else 'none')]
    lines += ['Review only. Nothing changed. The owner changes this in setup.']
    return '\n'.join(lines)


def main(argv=None):
    return run_cli(render, argv)


if __name__ == '__main__':
    sys.exit(main())
