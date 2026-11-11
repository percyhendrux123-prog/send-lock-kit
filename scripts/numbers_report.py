"""Optional complete, business-scoped histories for read-only numbers.

The load(business, now) contract and base collections match rundown. Existing
service adapters still work: unsupported histories are absent, never empty.
"""
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from rundown import FixtureAdapter as RundownFixtures, Snapshot, clean, day, instant, validate_businesses

NA = 'not available'
CLOSED = ('closed', 'complete', 'completed', 'done')


@dataclass
class NumbersSnapshot(Snapshot):
    numbers: dict = field(default_factory=dict)


class FixtureAdapter(RundownFixtures):
    def load(self, business, now):
        base = super().load(business, now)
        path = self.directory/'numbers.json'
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get(business['name'], {}), dict):
            raise ValueError('Numbers must be grouped by business name')
        return NumbersSnapshot(base.events, base.tasks, base.mail, data.get(business['name'], {}))


def rows(snapshot, name):
    values = getattr(snapshot, 'numbers', {})[name]
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise ValueError('Missing collection')
    return values


def amount(record, business):
    if record['currency'] != business.get('currency', 'USD') or isinstance(record['amount'], bool):
        raise ValueError('Different or missing currency')
    number = Decimal(str(record['amount']))
    if not number.is_finite() or number < 0 or number.as_tuple().exponent < -2:
        raise ValueError('Expected nonnegative money with at most two decimal places')
    return number


def available(calculate):
    try:
        return calculate()
    except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation, OverflowError):
        return None


def money(value, business):
    return NA if value is None else f"{business.get('currency', 'USD')} {value:.2f}"


def count(value):
    return NA if value is None else str(value)


class InvoiceIdentityError(ValueError):
    pass


def invoice_rows(snapshot):
    by_id = {}
    for invoice in rows(snapshot, 'invoices'):
        key = invoice.get('id')
        if not isinstance(key, str) or not key.strip():
            raise InvoiceIdentityError('An invoice is missing its ID.')
        if key in by_id:
            raise InvoiceIdentityError('An invoice ID is repeated.')
        by_id[key] = invoice
    return by_id


def payment_rows(snapshot, invoices):
    payments = rows(snapshot, 'payments')
    for payment in payments:
        key = payment.get('invoice_id')
        if not isinstance(key, str) or key not in invoices:
            raise InvoiceIdentityError('A payment needs a link to a known invoice.')
    return payments


def money_issue(snapshot):
    # One owner-facing explanation, not one repeated warning per calculation.
    try:
        payment_rows(snapshot, invoice_rows(snapshot))
    except InvoiceIdentityError as error:
        return str(error)+' Affected money figures are not available.'
    except (KeyError, ValueError, TypeError, AttributeError):
        pass
    return None


def flow(snapshot, name, start, end, business=None):
    if name == 'invoices':
        records = invoice_rows(snapshot).values()
    elif name == 'payments':
        records = payment_rows(snapshot, invoice_rows(snapshot))
    else:
        records = rows(snapshot, name)
    # Validate the complete collection, including records outside the window.
    dated = [(instant(r['at']), amount(r, business) if business else 1) for r in records]
    return sum((n for at, n in dated if start <= at < end), Decimal(0) if business else 0)


def balances(snapshot, business, point, overdue_only=False):
    zone = ZoneInfo(business.get('timezone', 'UTC'))
    invoices = invoice_rows(snapshot)
    payments = payment_rows(snapshot, invoices)
    by_id = {key: (instant(invoice['at']), amount(invoice, business)) for key, invoice in invoices.items()}
    paid = {key: Decimal(0) for key in by_id}
    for payment in payments:
        at, value = instant(payment['at']), amount(payment, business)
        key = payment['invoice_id']
        issued, _ = by_id[key]
        if at < issued:
            raise ValueError('Payment before invoice')
        if at <= point:
            paid[key] += value
    days = business.get('invoice_overdue_days')
    if overdue_only and (type(days) is not int or days < 0):
        raise ValueError('Missing invoice limit')
    result = Decimal(0)
    for key, (issued, value) in by_id.items():
        if paid[key] > value:
            raise ValueError('Overpayment needs an owner check')
        if issued <= point and (not overdue_only or
                (point.astimezone(zone).date()-issued.astimezone(zone).date()).days >= days):
            result += value-paid[key]
    return result


def late_tasks(snapshot, business, point):
    # Current task status alone cannot reconstruct last week's number.
    if getattr(snapshot, 'numbers', {}).get('task_history_complete') is not True:
        raise ValueError('No task history')
    zone = ZoneInfo(business.get('timezone', 'UTC'))
    total = 0
    for task in snapshot.tasks:
        if task['list_id'] not in business['clickup_list_ids']:
            continue
        created = instant(task['created_at'])
        closed = instant(task['completed_at']) if task.get('completed_at') else None
        if task['status'].lower() in CLOSED and closed is None:
            raise ValueError('Missing completion time')
        if closed and closed < created:
            raise ValueError('Task completion before creation')
        if created <= point and (closed is None or closed > point) and task.get('due'):
            total += day(task['due'], zone) < point.astimezone(zone).date()
    return total


def run_cli(render, argv=None):
    parser = argparse.ArgumentParser(description='Read-only numbers. Missing data is not available.')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--fixtures', type=Path)
    modes.add_argument('--live', action='store_true')
    parser.add_argument('--businesses', type=Path)
    parser.add_argument('--now', type=instant)
    args = parser.parse_args(argv)
    if args.live and not args.businesses:
        parser.error('--live requires an explicit --businesses configuration')
    now = args.now or datetime.now(timezone.utc)
    try:
        config = args.businesses or args.fixtures/'businesses.json'
        businesses = validate_businesses(json.loads(config.read_text()))
        if args.fixtures:
            adapter = FixtureAdapter(args.fixtures)
        else:
            from rundown_adapters import ServiceAdapter
            adapter = ServiceAdapter()
        output = '\n\n'.join(render(b, adapter.load(b, now), now) for b in businesses)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print('Numbers unavailable. Check the files, business settings and dates. No partial report produced.', file=sys.stderr)
        return 2
    print(output)
    return 0
