#!/usr/bin/env python3
"""Five weekly numbers, with the preceding week beside each one."""
from datetime import timedelta
import sys
from zoneinfo import ZoneInfo
from numbers_report import available, balances, clean, count, flow, late_tasks, money, money_issue, run_cli


def render(business, snapshot, now):
    local = now.astimezone(ZoneInfo(business.get('timezone', 'UTC')))
    last = local-timedelta(days=7)
    before = last-timedelta(days=7)
    values = []
    for start, end in ((last, local), (before, last)):
        values.append([
            count(available(lambda: flow(snapshot, 'leads', start, end))),
            count(available(lambda: flow(snapshot, 'replies', start, end))),
            money(available(lambda: flow(snapshot, 'payments', start, end, business)), business),
            money(available(lambda: balances(snapshot, business, end, True)), business),
            count(available(lambda: late_tasks(snapshot, business, end))),
        ])
    lines = [f"{clean(business['name'])} — weekly numbers", f"Seven days ending {local.isoformat()}.",
             'Last week is the preceding seven days. Overdue and late are balances at each end.', '']
    for name, current, previous in zip(('New leads', 'Replies sent', 'Money in', 'Money overdue', 'Tasks late'), *values):
        lines.append(f'{name}: {current} | last week: {previous}')
    issue = money_issue(snapshot)
    if issue:
        lines.append(issue)
    return '\n'.join(lines)


def main(argv=None):
    return run_cli(render, argv)


if __name__ == '__main__':
    sys.exit(main())
