#!/usr/bin/env python3
"""Install trust regressions: real fake-only trees; UID metadata stubbed only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy/one_door'))
import install


class TrustTests(unittest.TestCase):
    def stage(self, directory):
        root = Path(directory)/'stage'
        root.mkdir(mode=0o755)
        for name in ('releases', 'backups'):
            (root/name).mkdir(mode=0o755)
        return root

    def invoke(self, root, *action):
        with patch.object(sys, 'argv', ['install.py', '--root', str(root), '--rehearsal', *action]), contextlib.redirect_stdout(io.StringIO()) as output:
            install.main()
        return json.loads(output.getvalue())

    @contextlib.contextmanager
    def unsafe(self, path, form):
        if form == 'symlink':
            moved = path.with_name(path.name+'-moved')
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=moved.is_dir())
        elif form in ('group-write', 'world-write'):
            path.chmod(path.stat().st_mode | (0o020 if form == 'group-write' else 0o002))
        real = Path.lstat
        def metadata(p, *args, **kw):
            value = real(p, *args, **kw)
            if p == path and form == 'wrong-owner':
                fields = list(value)
                fields[4] = os.geteuid()+1
                return os.stat_result(fields)
            return value
        with patch.object(Path, 'lstat', metadata):
            yield

    def test_safe_root_rejects_unsafe_reserved_child(self):
        for name in ('releases', 'backups'):
            for form in ('group-write', 'world-write', 'symlink', 'wrong-owner'):
                with self.subTest(child=name, form=form), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                    root = self.stage(directory)
                    with self.unsafe(root/name, form):
                        with self.assertRaises((ValueError, OSError, SystemExit)):
                            self.invoke(root, 'install', '--kit', str(ROOT))
                    self.assertFalse((root/'current').exists())

    def test_noop_and_reuse_reject_unsafe_nested_release(self):
        for reuse in (False, True):
            for name in ('', 'guard', 'guard/plugins', 'guard/plugins/send-lock', 'code'):
                for form in ('group-write', 'world-write', 'symlink', 'wrong-owner'):
                    with self.subTest(reuse=reuse, child=name, form=form), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                        root = self.stage(directory)
                        first = install.install(root, ROOT)
                        if reuse:
                            (root/'current').unlink()
                        with self.unsafe(root/first['release']/name, form):
                            with self.assertRaises((ValueError, OSError, SystemExit)):
                                self.invoke(root, 'install', '--kit', str(ROOT))
                        self.assertEqual(os.readlink(root/'current') if (root/'current').is_symlink() else None,
                                         None if reuse else first['release'])

    def test_restore_rejects_unsafe_previous_release_and_backup(self):
        for name in ('release', 'guard', 'backups', 'record'):
            for form in ('group-write', 'world-write', 'symlink', 'wrong-owner'):
                with self.subTest(child=name, form=form), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                    root = self.stage(directory)
                    first = install.install(root, ROOT)
                    real = install.read_regular
                    def upgrade(path):
                        raw = real(path)
                        return raw+b'\n# fake upgrade\n' if path == ROOT/'plugins/send-lock/__init__.py' else raw
                    with patch.object(install, 'read_regular', upgrade):
                        second = install.install(root, ROOT)
                    paths = {'release': root/first['release'], 'guard': root/first['release']/'guard',
                             'backups': root/'backups', 'record': root/'backups'/second['backup']}
                    with self.unsafe(paths[name], form):
                        with self.assertRaises((ValueError, OSError, SystemExit)):
                            self.invoke(root, 'restore', '--backup', second['backup'])
                    self.assertEqual(os.readlink(root/'current'), second['release'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
