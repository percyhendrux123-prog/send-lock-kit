"""Read-only service adapters. No OAuth, refresh, sends or credential files.

Google requires an owner-supplied short-lived GOOGLE_ACCESS_TOKEN with
Gmail readonly + Calendar readonly scopes. mail_labels are explicit waiting
label IDs, not inferred promises that a thread needs a reply. Selection is OR
across labels; the latest non-draft message decides who is waiting.
"""
import json
import os
from datetime import datetime, time, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request
from zoneinfo import ZoneInfo

from clickup import ClickUpClient, open_http
from rundown import Snapshot


class GoogleReadClient:
    def __init__(self, token=None):
        self._token = os.environ.get('GOOGLE_ACCESS_TOKEN', '') if token is None else token

    def get(self, service, path, query):
        bases = {'gmail': 'https://gmail.googleapis.com/gmail/v1/users/me',
                 'calendar': 'https://www.googleapis.com/calendar/v3'}
        if not self._token or any(c in self._token for c in '\r\n'):
            raise ValueError('Owner must supply GOOGLE_ACCESS_TOKEN; no login attempted')
        url = bases[service] + path + ('?' + urlencode(query, doseq=True) if query else '')
        try:
            with open_http(Request(url, headers={'Authorization': 'Bearer ' + self._token}, method='GET'), timeout=30) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError('Unexpected Google collection')
            return result
        except HTTPError as exc:
            exc.close()
            raise ValueError('Google read failed; check the owner consent and service status') from None
        except (URLError, OSError, ValueError):
            raise ValueError('Google read failed; check the owner consent and service status') from None


def pages(google, service, path, query, key):
    query = dict(query)
    seen = set()
    while True:
        result = google.get(service, path, dict(query))
        rows = result.get(key, [])
        if not isinstance(rows, list):
            raise ValueError('Invalid Google collection')
        yield from rows
        token = result.get('nextPageToken')
        if not token:
            return
        if token in seen or len(seen) >= 10000:
            raise ValueError('Google pagination incomplete')
        seen.add(token)
        query['pageToken'] = token


class CalendarReader:
    def __init__(self, google):
        self.google = google

    def read(self, business, now):
        zone = ZoneInfo(business.get('timezone', 'UTC'))
        start = datetime.combine(now.astimezone(zone).date(), time(), zone)
        rows = []
        for calendar_id in business['calendar_ids']:
            query = {'timeMin': start.isoformat(), 'timeMax': (start + timedelta(days=1)).isoformat(),
                     'singleEvents': 'true', 'orderBy': 'startTime', 'maxResults': 2500}
            path = '/calendars/' + quote(calendar_id, safe='') + '/events'
            for event in pages(self.google, 'calendar', path, query, 'items'):
                if event.get('status') == 'cancelled':
                    continue
                rows.append({'id': event['id'], 'calendar_id': calendar_id,
                             'summary': event.get('summary', '(untitled event)'),
                             'start': event['start'].get('dateTime') or event['start']['date'],
                             'end': event['end'].get('dateTime') or event['end']['date']})
        return rows


def milliseconds(value):
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()


class GmailReader:
    def __init__(self, google):
        self.google = google

    def read(self, business, now):
        threads = {}
        for label in business['mail_labels']:
            for thread in pages(self.google, 'gmail', '/threads',
                                {'labelIds': label, 'maxResults': 100}, 'threads'):
                threads.setdefault(thread['id'], set()).add(label)
        rows = []
        for thread_id, labels in sorted(threads.items()):
            result = self.google.get('gmail', '/threads/' + quote(thread_id, safe=''),
                                     {'format': 'metadata', 'metadataHeaders': ['Subject']})
            messages = [m for m in result.get('messages', [])
                        if not set(m.get('labelIds', [])).intersection(('DRAFT', 'SPAM', 'TRASH'))
                        and int(m['internalDate']) / 1000 <= now.timestamp()]
            if not messages:
                continue
            latest = max(messages, key=lambda m: (int(m['internalDate']), m['id']))
            subject = next((h['value'] for h in latest.get('payload', {}).get('headers', [])
                            if h['name'].lower() == 'subject'), '(no subject)')
            rows.append({'id': thread_id, 'labels': sorted(labels), 'subject': subject,
                         'last_message_at': milliseconds(latest['internalDate']),
                         'waiting_on_reply': True,
                         'waiting_on': 'customer' if 'SENT' in latest.get('labelIds', []) else 'owner'})
        return rows


class ClickUpReader:
    def __init__(self, client):
        self.client = client

    def read(self, business, now):
        rows = []
        for list_id in business['clickup_list_ids']:
            for task in self.client.get_tasks(list_id):
                status = task['status']
                rows.append({'id': task['id'], 'list_id': list_id, 'name': task['name'],
                             'status': 'closed' if status.get('type') in ('closed', 'done') else status['status'],
                             'due': milliseconds(task['due_date']) if task.get('due_date') else None,
                             'updated_at': milliseconds(task.get('date_updated') or task['date_created'])})
        return rows


class ServiceAdapter:
    def __init__(self, calendar=None, clickup=None, gmail=None):
        google = GoogleReadClient()
        self.calendar = calendar if calendar is not None else CalendarReader(google)
        self.clickup = clickup if clickup is not None else ClickUpReader(ClickUpClient())
        self.gmail = gmail if gmail is not None else GmailReader(google)

    def load(self, business, now):
        return Snapshot(self.calendar.read(business, now), self.clickup.read(business, now), self.gmail.read(business, now))
