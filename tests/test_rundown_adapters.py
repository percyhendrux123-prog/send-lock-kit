#!/usr/bin/env python3
"""Mock-HTTP tests for the live-reader adapter; never loads credentials or networks."""
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from rundown import instant

CFG = {'name': 'Northside Studio', 'timezone': 'America/Chicago', 'calendar_ids': ['studio@example.com'],
       'clickup_list_ids': ['10'], 'mail_labels': ['Waiting_Northside']}
NOW = instant('2026-11-10T08:00:00-06:00')


class AdapterTests(unittest.TestCase):
    def test_google_get_only_and_error_sanitization(self):
        import rundown_adapters as adapters
        from urllib.error import HTTPError
        response = io.BytesIO(b'{"items":[]}')
        with patch('rundown_adapters.open_http', return_value=response) as http:
            self.assertEqual(adapters.GoogleReadClient('fake-google-token').get('calendar', '/calendars/a/events', {'singleEvents': 'true'}), {'items': []})
        req = http.call_args.args[0]
        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.get_header('Authorization'), 'Bearer fake-google-token')
        self.assertTrue(req.full_url.startswith('https://www.googleapis.com/calendar/v3/'))
        self.assertTrue(response.closed)
        exc = HTTPError('', 403, 'fake-google-token', {}, io.BytesIO(b'fake-google-token'))
        with patch('rundown_adapters.open_http', side_effect=exc):
            with self.assertRaisesRegex(ValueError, 'Google read failed') as error:
                adapters.GoogleReadClient('fake-google-token').get('gmail', '/threads', {})
        self.assertNotIn('fake-google-token', str(error.exception))
        self.assertTrue(exc.closed)
        with patch('rundown_adapters.open_http') as http:
            with self.assertRaises(ValueError):
                adapters.GoogleReadClient('').get('gmail', '/threads', {})
        http.assert_not_called()

    def test_calendar_pagination_and_timezone_window(self):
        import rundown_adapters as adapters
        google = Mock()
        google.get.side_effect = [
            {'items': [{'id': 'a', 'summary': 'Mobility', 'start': {'dateTime': '2026-11-10T09:00:00-06:00'},
                        'end': {'dateTime': '2026-11-10T10:00:00-06:00'}}], 'nextPageToken': 'page2'},
            {'items': [{'id': 'b', 'summary': 'Studio day', 'start': {'date': '2026-11-10'}, 'end': {'date': '2026-11-11'}},
                       {'id': 'c', 'status': 'cancelled'}]}]
        rows = adapters.CalendarReader(google).read(CFG, NOW)
        self.assertEqual([r['id'] for r in rows], ['a', 'b'])
        self.assertEqual(rows[0]['calendar_id'], 'studio@example.com')
        query = google.get.call_args_list[0].args[2]
        self.assertEqual(query['timeMin'], '2026-11-10T00:00:00-06:00')
        self.assertEqual(query['timeMax'], '2026-11-11T00:00:00-06:00')
        self.assertEqual(google.get.call_args_list[1].args[2]['pageToken'], 'page2')
        self.assertIn('studio%40example.com', google.get.call_args_list[0].args[1])

    def test_gmail_deduplicates_labels_and_uses_latest_message_not_first(self):
        import rundown_adapters as adapters
        cfg = dict(CFG, mail_labels=['Waiting_Northside', 'Studio'])
        google = Mock()
        def get(service, path, query):
            if path == '/threads':
                if query.get('pageToken'):
                    return {'threads': [{'id': 'a'}]}
                return {'threads': [{'id': 'a'}], 'nextPageToken': 'p2'}
            return {'messages': [
                {'id': 'm1', 'internalDate': '1794146400000', 'labelIds': ['INBOX'],
                 'payload': {'headers': [{'name': 'Subject', 'value': 'Old question'}]}},
                {'id': 'm2', 'internalDate': '1794232800000', 'labelIds': ['SENT'],
                 'payload': {'headers': [{'name': 'Subject', 'value': 'Owner replied'}]}},
                {'id': 'draft', 'internalDate': '1794319200000', 'labelIds': ['DRAFT']} ]}
        google.get.side_effect = get
        rows = adapters.GmailReader(google).read(cfg, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['subject'], 'Owner replied')
        self.assertEqual(rows[0]['waiting_on'], 'customer')
        self.assertEqual(rows[0]['labels'], ['Studio', 'Waiting_Northside'])
        self.assertEqual(rows[0]['last_message_at'], '2026-11-09T14:00:00+00:00')
        self.assertEqual(sum(c.args[1] == '/threads/a' for c in google.get.call_args_list), 1)
        self.assertEqual(google.get.call_args_list[-1].args[2]['format'], 'metadata')

    def test_clickup_normalizes_milliseconds_and_closed_type(self):
        import rundown_adapters as adapters
        client = Mock()
        client.get_tasks.return_value = [
            {'id': 'a', 'name': 'Mat check', 'status': {'status': 'Finished', 'type': 'closed'},
             'due_date': '1794319200000', 'date_updated': '1794146400000'},
            {'id': 'b', 'name': 'Plan class', 'status': {'status': 'in progress', 'type': 'custom'},
             'due_date': None, 'date_updated': '1794232800000'}]
        rows = adapters.ClickUpReader(client).read(CFG, NOW)
        self.assertEqual(rows[0]['status'], 'closed')
        self.assertEqual(rows[0]['list_id'], '10')
        self.assertEqual(rows[0]['due'], '2026-11-10T14:00:00+00:00')
        self.assertIsNone(rows[1]['due'])
        self.assertEqual(rows[1]['status'], 'in progress')

    def test_service_adapter_implements_same_load_contract(self):
        import rundown_adapters as adapters
        readers = [Mock(), Mock(), Mock()]
        for i, reader in enumerate(readers):
            reader.read.return_value = [i]
        snapshot = adapters.ServiceAdapter(*readers).load(CFG, NOW)
        self.assertEqual((snapshot.events, snapshot.tasks, snapshot.mail), ([0], [1], [2]))
        for reader in readers:
            reader.read.assert_called_once_with(CFG, NOW)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    failures = len(result.failures) + len(result.errors)
    print('rundown adapters: %d passed, %d failed' % (result.testsRun - failures, failures))
    sys.exit(0 if result.wasSuccessful() else 1)
