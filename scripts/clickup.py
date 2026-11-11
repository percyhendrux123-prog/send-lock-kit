#!/usr/bin/env python3
"""ClickUp API v2 helper, stdlib only. Writes require a fresh owner confirmation.

CLICKUP_TOKEN is read from the environment, never from argv or a file. Dry-run
never opens a socket and works without credentials. This is an owner-operated
CLI gate, NOT a substitute for Hermes-wide outbound authorization.
"""
import argparse
import copy
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = 'https://api.clickup.com/api/v2'


class ClickUpError(ValueError):
    """Safe diagnostic, without response bodies, URLs or credentials."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError('', code, 'Redirect refused', headers, fp)


def open_http(request, timeout=30):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def redact(value, token):
    """Redact token echoes even inside task fields, response objects and dry runs."""
    if isinstance(value, str):
        return value.replace(token, '[REDACTED]') if token else value
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {redact(str(k), token): redact(v, token) for k, v in value.items()}
    return value


def identifier(value):
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ClickUpError('Expected an alphanumeric ClickUp ID')
    return value


class ClickUpClient:
    def __init__(self, token=None, *, dry_run=False, approve=None):
        self._token = os.environ.get('CLICKUP_TOKEN', '') if token is None else token
        self.dry_run = dry_run
        self.approve = approve

    def _request(self, method, path, body=None, query=None):
        # Serialize BEFORE confirmation: the owner approves the exact immutable bytes.
        url = API + path + ('?' + urlencode(query) if query else '')
        data = json.dumps(body, sort_keys=True, allow_nan=False).encode() if body is not None else None
        preview = redact({'method': method, 'url': url,
                          'headers': {'Authorization': '[REDACTED]', 'Content-Type': 'application/json'},
                          'body': json.loads(data) if data else None}, self._token)
        if self.dry_run:
            return preview
        if not self._token or any(c in self._token for c in '\r\n'):
            raise ClickUpError('Set CLICKUP_TOKEN in the owner environment')
        if method != 'GET' and (self.approve is None or self.approve(copy.deepcopy(preview)) is not True):
            raise ClickUpError('Owner approval required for each write; use --dry-run to preview')
        request = Request(url, data=data, method=method,
                          headers={'Authorization': self._token, 'Content-Type': 'application/json'})
        try:
            with open_http(request, timeout=30) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError('Unexpected response')
            return redact(result, self._token)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ClickUpError('ClickUp HTTP %s; no automatic retries (write may be uncertain)' % status) from None
        except (URLError, OSError, ValueError):
            raise ClickUpError('ClickUp request failed; no automatic retries (write may be uncertain)') from None

    def get_task(self, task_id):
        return self._request('GET', '/task/' + identifier(task_id))

    def get_tasks(self, list_id):
        path = '/list/' + identifier(list_id) + '/task'
        if self.dry_run:
            return self._request('GET', path, query={'page': 0, 'include_closed': 'false'})
        tasks = []
        for page in range(10000):
            result = self._request('GET', path, query={'page': page, 'include_closed': 'false'})
            batch = result.get('tasks')
            if not isinstance(batch, list) or any(not isinstance(t, dict) for t in batch):
                raise ClickUpError('ClickUp returned an invalid tasks collection')
            tasks.extend(batch)
            if result.get('last_page') is True or not batch:
                return tasks
        raise ClickUpError('ClickUp pagination limit reached; refusing incomplete result')

    def get_lists(self, parent_id, *, kind='folder'):
        if kind not in ('folder', 'space'):
            raise ClickUpError('Lists parent must be a folder or space')
        result = self._request('GET', '/' + kind + '/' + identifier(parent_id) + '/list')
        if self.dry_run:
            return result
        if not isinstance(result.get('lists'), list):
            raise ClickUpError('ClickUp returned an invalid lists collection')
        return result['lists']

    def create_task(self, list_id, data):
        if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not data['name'].strip():
            raise ClickUpError('Create requires a JSON object with a nonempty name')
        return self._request('POST', '/list/' + identifier(list_id) + '/task', data)

    def update_task(self, task_id, data):
        if not isinstance(data, dict) or not data:
            raise ClickUpError('Update requires a nonempty JSON object')
        return self._request('PUT', '/task/' + identifier(task_id), data)


def confirm_owner(preview):
    if not sys.stdin.isatty():
        return False
    print(json.dumps(preview, indent=2, sort_keys=True))
    try:
        return input('Owner: type APPROVE to execute this one exact request: ') == 'APPROVE'
    except EOFError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    subs = parser.add_subparsers(dest='action', required=True)
    for action in ('task', 'tasks', 'lists', 'create', 'update'):
        sub = subs.add_parser(action)
        sub.add_argument('id')
        sub.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS)
        if action == 'lists':
            sub.add_argument('--kind', choices=('folder', 'space'), default='folder')
        if action in ('create', 'update'):
            sub.add_argument('--data', required=True, help='JSON object; never put credentials in task data')
    args = parser.parse_args(argv)
    client = ClickUpClient(dry_run=args.dry_run, approve=confirm_owner)
    try:
        if args.action in ('create', 'update'):
            data = json.loads(args.data)
            result = (client.create_task if args.action == 'create' else client.update_task)(args.id, data)
        elif args.action == 'lists':
            result = client.get_lists(args.id, kind=args.kind)
        else:
            result = (client.get_task if args.action == 'task' else client.get_tasks)(args.id)
        print(json.dumps(redact(result, client._token), indent=2, sort_keys=True, allow_nan=False))
        return 0
    except ClickUpError as exc:
        print(str(exc), file=sys.stderr)
    except (ValueError, TypeError):
        print('Invalid JSON data; expected a task object', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
