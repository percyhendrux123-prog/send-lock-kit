#!/usr/bin/env python3
"""Install retry regressions. No processes/services/network; injected disk failures."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy/one_door'))
import install


class RetryTests(unittest.TestCase):
    def exercise(self, upgrade, operation, fail_at):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)/'stage'
            root.mkdir()
            for name in ('releases', 'backups'):
                (root/name).mkdir()
            previous = install.install(root, ROOT)['release'] if upgrade else None
            before = {p.name for p in (root/'releases').iterdir()}
            read = install.read_regular
            def source(path):
                raw = read(path)
                return raw+b'\n# synthetic upgrade\n' if upgrade and path == ROOT/'plugins/send-lock/__init__.py' else raw
            real = install.atomic if operation == 'atomic' else Path.chmod
            calls = 0
            def interrupted(*args, **kwargs):
                nonlocal calls
                # Count release-file chmod, not private directory setup.
                count = operation == 'atomic' or args[1] == 0o444
                if count:
                    calls += 1
                    if calls == fail_at:
                        raise OSError('injected '+operation+' interruption')
                return real(*args, **kwargs)
            with patch.object(install, 'read_regular', source):
                target = install if operation == 'atomic' else Path
                attr = 'atomic' if operation == 'atomic' else 'chmod'
                with patch.object(target, attr, interrupted):
                    with self.assertRaisesRegex(OSError, 'injected'):
                        install.install(root, ROOT)
                self.assertEqual(os.readlink(root/'current') if (root/'current').is_symlink() else None, previous)
                # Any final fingerprint directory must be complete, even if the
                # failure happened while writing the backup after publication.
                for release in (root/'releases').iterdir():
                    if len(release.name) == 64:
                        for name in ['code/'+n for n in ('sender.py', 'gateway.py', 'owner.py')] + ['guard/'+n for n in ('config.yaml', 'axiom-owner.json', 'egress.json')] + ['guard/plugins/send-lock/'+n for n in ('__init__.py', 'plugin.yaml')]:
                            self.assertTrue((release/name).is_file(), 'partial published release: '+name)
                try:
                    retry = install.install(root, ROOT)
                    repeat = install.install(root, ROOT)
                except (OSError, ValueError) as error:
                    self.fail('identical retry/repeat failed: '+repr(error))
                self.assertEqual(retry['action'], 'installed')
                self.assertEqual(install.pointer(root), retry['release'])
                self.assertEqual(repeat['action'], 'unchanged')
                self.assertEqual(len(list((root/'backups').glob('*.json'))), 2 if upgrade else 1)
                if upgrade:
                    self.assertIn(previous.split('/')[1], before)

    def test_first_install_retry_after_each_write(self):
        for fail_at in range(1, 10):  # Eight payload files, then the backup.
            with self.subTest(write=fail_at):
                self.exercise(False, 'atomic', fail_at)

    def test_upgrade_retry_after_each_write_preserves_current(self):
        for fail_at in range(1, 10):
            with self.subTest(write=fail_at):
                self.exercise(True, 'atomic', fail_at)

    def test_promotion_fsync_failure_preserves_previous_current(self):
        for upgrade in (False, True):
            with self.subTest(upgrade=upgrade), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                root = Path(directory)/'stage'
                root.mkdir()
                for name in ('releases', 'backups'):
                    (root/name).mkdir()
                old = install.install(root, ROOT)['release'] if upgrade else None
                read = install.read_regular
                def source(path):
                    raw = read(path)
                    return raw+b'\n# fake upgrade\n' if upgrade and path == ROOT/'plugins/send-lock/__init__.py' else raw
                real = install.os.fsync
                failed = False
                def fail_root_sync(fd):
                    nonlocal failed
                    if not failed and install.os.fstat(fd).st_ino == root.stat().st_ino:
                        failed = True
                        raise OSError('injected promotion sync failure')
                    return real(fd)
                with patch.object(install, 'read_regular', source):
                    with patch.object(install.os, 'fsync', fail_root_sync):
                        with self.assertRaisesRegex(OSError, 'promotion sync'):
                            install.install(root, ROOT)
                    self.assertTrue(failed)
                    self.assertEqual(install.pointer(root), old)
                    self.assertEqual(install.install(root, ROOT)['action'], 'installed')

    def test_retry_after_each_file_chmod(self):
        for upgrade in (False, True):
            for fail_at in range(1, 9):
                with self.subTest(upgrade=upgrade, chmod=fail_at):
                    self.exercise(upgrade, 'chmod', fail_at)


if __name__ == '__main__':
    unittest.main(verbosity=2)
