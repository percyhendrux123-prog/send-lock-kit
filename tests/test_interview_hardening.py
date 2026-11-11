#!/usr/bin/env python3
"""Page rollback regressions on disposable fake owner pages."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import interview


class RollbackTests(unittest.TestCase):
    def test_rollback_attempts_every_page_and_names_each_failure(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                guard = Path(directory)
                payloads = {name: b'NEW '+name.encode() for name in ('COMPANY.md', 'APPROVALS.md', 'PRICES.md')}
                before = {name: b'BEFORE '+name.encode() for name in payloads} if existing else {}
                for name, raw in before.items():
                    (guard/name).write_bytes(raw)
                replace, unlink = os.replace, Path.unlink
                def fail_replace(source, destination):
                    if Path(destination) == guard/'PRICES.md' and Path(source).name == 'PRICES.md':
                        raise OSError('injected write failure')
                    if Path(source).name == 'before-APPROVALS.md':
                        raise OSError('injected restore failure')
                    return replace(source, destination)
                def fail_unlink(path, *args, **kwargs):
                    if path == guard/'APPROVALS.md':
                        raise OSError('injected remove failure')
                    return unlink(path, *args, **kwargs)
                with patch('interview.os.replace', side_effect=fail_replace), \
                        patch.object(Path, 'unlink', fail_unlink), \
                        contextlib.redirect_stderr(io.StringIO()) as error:
                    with self.assertRaisesRegex(OSError, 'injected write failure'):
                        interview.write_pages(guard, payloads)
                self.assertEqual((guard/'COMPANY.md').read_bytes() if existing else (guard/'COMPANY.md').exists(),
                                 before.get('COMPANY.md', False))
                self.assertEqual((guard/'PRICES.md').read_bytes() if existing else (guard/'PRICES.md').exists(),
                                 before.get('PRICES.md', False))
                self.assertEqual((guard/'APPROVALS.md').read_bytes(), payloads['APPROVALS.md'])
                self.assertIn('APPROVALS.md', error.getvalue())
                stages = list(guard.glob('.interview-*'))
                self.assertEqual(len(stages), 1)
                self.assertIn(str(stages[0]), error.getvalue())
                if existing:
                    self.assertEqual((stages[0]/'before-APPROVALS.md').read_bytes(), before['APPROVALS.md'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
