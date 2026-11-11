#!/usr/bin/env python3
"""Offline ClickUp contract tests. No HTTP leaves this process."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class ClickUpTests(unittest.TestCase):
    def test_task_read_uses_v2_and_authorization(self):
        import clickup
        response = io.BytesIO(b'{"id":"fake123","name":"Mat check"}')
        with patch('clickup.open_http', return_value=response) as http:
            client = clickup.ClickUpClient('fake-test-token')
            result = client.get_task('fake123')
        self.assertEqual(result['name'], 'Mat check')
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.clickup.com/api/v2/task/fake123')
        self.assertEqual(request.get_header('Authorization'), 'fake-test-token')
        self.assertEqual(request.method, 'GET')
        self.assertTrue(response.closed)


    def test_write_default_denies_without_touching_http(self):
        import clickup
        with patch('clickup.open_http') as http:
            with self.assertRaisesRegex(clickup.ClickUpError, 'approval'):
                clickup.ClickUpClient('fake-test-token').create_task('10', {'name': 'Mat check'})
        http.assert_not_called()

    def test_dry_run_redacts_everywhere_without_token_or_network(self):
        import clickup
        out = io.StringIO()
        with patch.dict('os.environ', {'CLICKUP_TOKEN': 'fake-test-token'}, clear=True), \
                patch('clickup.open_http') as http, patch('sys.stdout', out):
            code = clickup.main(['--dry-run', 'create', '10', '--data',
                                 '{"name":"fake-test-token mat check"}'])
        self.assertEqual(code, 0)
        self.assertNotIn('fake-test-token', out.getvalue())
        request = json.loads(out.getvalue())
        self.assertEqual(request['headers']['Authorization'], '[REDACTED]')
        self.assertEqual(request['method'], 'POST')
        self.assertIn('[REDACTED]', request['body']['name'])
        http.assert_not_called()

    def test_approved_create_and_update_use_exact_json(self):
        import clickup
        approved = []
        client = clickup.ClickUpClient('fake-test-token', approve=lambda r: approved.append(r) or True)
        with patch('clickup.open_http', side_effect=[io.BytesIO(b'{"id":"a"}'), io.BytesIO(b'{"id":"a"}')]) as http:
            client.create_task('10', {'name': 'Mat check'})
            client.update_task('a', {'status': 'in progress'})
        self.assertEqual([c.args[0].method for c in http.call_args_list], ['POST', 'PUT'])
        self.assertEqual(json.loads(http.call_args_list[0].args[0].data), {'name': 'Mat check'})
        self.assertEqual(approved[1]['body'], {'status': 'in progress'})
        self.assertNotIn('fake-test-token', json.dumps(approved))

    def test_task_pagination_and_lists(self):
        import clickup
        client = clickup.ClickUpClient('fake-test-token')
        with patch('clickup.open_http', side_effect=[
            io.BytesIO(b'{"tasks":[{"id":"a"}],"last_page":false}'),
            io.BytesIO(b'{"tasks":[{"id":"b"}],"last_page":true}'),
            io.BytesIO(b'{"lists":[{"id":"10"}]}'),
        ]) as http:
            self.assertEqual([x['id'] for x in client.get_tasks('10')], ['a', 'b'])
            self.assertEqual(client.get_lists('20', kind='space'), [{'id': '10'}])
        self.assertIn('page=1', http.call_args_list[1].args[0].full_url)
        self.assertIn('/space/20/list', http.call_args_list[2].args[0].full_url)

    def test_bad_ids_payloads_and_absent_token_never_network(self):
        import clickup
        with patch('clickup.open_http') as http:
            for bad in ('../secret', 'a?token=x', 'a/b', ''):
                with self.assertRaises(clickup.ClickUpError):
                    clickup.ClickUpClient('fake').get_task(bad)
            with self.assertRaises(clickup.ClickUpError):
                clickup.ClickUpClient('').get_task('a')
            with self.assertRaises(clickup.ClickUpError):
                clickup.ClickUpClient('fake').create_task('10', {})
            with self.assertRaises(clickup.ClickUpError):
                clickup.ClickUpClient('fake').update_task('a', [])
        http.assert_not_called()

    def test_errors_are_sanitized_and_http_error_closed(self):
        import clickup
        from urllib.error import HTTPError, URLError
        for exc in (HTTPError('https://example.invalid', 401, 'fake-test-token', {}, io.BytesIO(b'fake-test-token')),
                    URLError('fake-test-token'), ValueError('fake-test-token')):
            with patch('clickup.open_http', side_effect=exc):
                with self.assertRaises(clickup.ClickUpError) as error:
                    clickup.ClickUpClient('fake-test-token').get_task('a')
            self.assertNotIn('fake-test-token', str(error.exception))
            if isinstance(exc, HTTPError):
                self.assertTrue(exc.closed)

    def test_unattended_write_refused_and_response_token_redacted(self):
        import clickup
        with patch.dict('os.environ', {'CLICKUP_TOKEN': 'fake-test-token'}, clear=True), \
                patch('clickup.open_http') as http, patch('sys.stdin.isatty', return_value=False), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            self.assertEqual(clickup.main(['create', '10', '--data', '{"name":"Mat check"}']), 2)
        http.assert_not_called()
        out = io.StringIO()
        with patch.dict('os.environ', {'CLICKUP_TOKEN': 'fake-test-token'}, clear=True), \
                patch('clickup.open_http', return_value=io.BytesIO(b'{"echo":"fake-test-token"}')), \
                patch('sys.stdout', out):
            self.assertEqual(clickup.main(['task', 'a']), 0)
        self.assertNotIn('fake-test-token', out.getvalue())

    def test_redirects_never_forward_token(self):
        import clickup
        handler = clickup.NoRedirect()
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError):
            handler.redirect_request(None, None, 302, 'redirect', {}, 'https://example.invalid')


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    failures = len(result.failures) + len(result.errors)
    print('clickup: %d passed, %d failed' % (result.testsRun - failures, failures))
    sys.exit(0 if result.wasSuccessful() else 1)
