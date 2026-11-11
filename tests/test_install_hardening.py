#!/usr/bin/env python3
"""Offline installer refusals and conservative temporary-tree cleanup."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy/one_door'))
import install


class RefusalTests(unittest.TestCase):
    def test_unsafe_folders_get_one_plain_line_without_traceback(self):
        for kind in ('root', 'ancestor-symlink', 'releases', 'backups', 'nested-release', 'lock-symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                base = Path(directory)
                root = base/'stage'
                root.mkdir()
                for name in ('releases', 'backups'):
                    (root/name).mkdir()
                selected = root
                if kind == 'ancestor-symlink':
                    link = base/'alias'
                    link.symlink_to(base, target_is_directory=True)
                    selected = link/'stage'
                    unsafe = link
                elif kind == 'lock-symlink':
                    unsafe = root/'.install.lock'
                    unsafe.symlink_to(base/'untouched')
                else:
                    unsafe = root if kind == 'root' else root/kind
                    if kind == 'nested-release':
                        unsafe = root/'releases'/'unpublished'
                        unsafe.mkdir()
                    unsafe.chmod(0o777)
                run = subprocess.run([sys.executable, str(ROOT/'deploy/one_door/install.py'),
                                      '--root', str(selected), '--rehearsal', 'install', '--kit', str(ROOT)],
                                     capture_output=True, text=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(run.stdout, '')
                self.assertEqual(len(run.stderr.splitlines()), 1, run.stderr)
                self.assertNotIn('Traceback', run.stderr)
                self.assertIn(str(unsafe), run.stderr)
                self.assertRegex(run.stderr, r'(?i)(symlink|writable|owner|directory)')


class CleanupTests(unittest.TestCase):
    def stage(self, directory):
        root = Path(directory)/'stage'
        root.mkdir(parents=True)
        for name in ('releases', 'backups'):
            (root/name).mkdir()
        return root

    def killed_install(self, root):
        # A real subprocess death skips TemporaryDirectory cleanup. No service
        # starts: the installer only writes fake staging files.
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); import install; "
                "from pathlib import Path; real=install.atomic; count=0\n"
                "def killed(*args, **kwargs):\n"
                " global count\n count += 1\n"
                " if count == 2: os._exit(91)\n"
                " return real(*args, **kwargs)\n"
                "install.atomic=killed\ninstall.install(Path(sys.argv[2]), Path(sys.argv[3]))")
        run = subprocess.run([sys.executable, '-c', code, str(ROOT/'deploy/one_door'), str(root), str(ROOT)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 91, run.stderr)
        leftovers = list((root/'releases').glob('.release-*'))
        self.assertEqual(len(leftovers), 1)
        return leftovers[0]

    def test_success_and_noop_clean_only_identified_old_private_trees(self):
        for noop in (False, True):
            with self.subTest(noop=noop), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                root = self.stage(directory)
                stale = self.killed_install(root)
                result = install.install(root, ROOT)
                self.assertFalse(stale.exists(), 'successful install left an identified stale private tree')
                release = root/result['release']
                self.assertTrue(release.is_dir())
                self.assertFalse((release/'.installer-private.json').exists())
                if noop:
                    stale = self.killed_install(self.stage(Path(directory)/'other'))
                    # Keep its inode/marker identity while moving within releases.
                    moved = root/'releases'/stale.name
                    stale.rename(moved)
                    self.assertEqual(install.install(root, ROOT)['action'], 'unchanged')
                    self.assertFalse(moved.exists())

    def test_cleanup_never_deletes_unmarked_new_unsafe_or_linked_trees(self):
        import shutil
        import time
        for form in ('unmarked', 'mode', 'wrong-owner', 'new', 'symlink', 'nested-symlink',
                     'marker-symlink', 'marker-hardlink', 'bad-marker', 'copied-marker', 'extra-file'):
            with self.subTest(form=form), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                root = self.stage(directory)
                stale = self.killed_install(root)
                marker = stale/'.installer-private.json'
                self.assertTrue(marker.exists())
                outside = Path(directory)/'untouched'
                outside.mkdir()
                (outside/'sentinel').write_bytes(b'KEEP')
                if form == 'extra-file':
                    (stale/'owner-note.txt').write_bytes(b'KEEP')
                elif form == 'unmarked':
                    marker.unlink()
                elif form == 'mode':
                    stale.chmod(0o755)
                elif form == 'new':
                    os.utime(stale, ns=(time.time_ns()+10**12, time.time_ns()+10**12))
                elif form == 'symlink':
                    shutil.rmtree(stale)
                    stale.symlink_to(outside, target_is_directory=True)
                elif form == 'nested-symlink':
                    (stale/'linked').symlink_to(outside, target_is_directory=True)
                elif form == 'marker-symlink':
                    marker.unlink()
                    marker.symlink_to(outside/'sentinel')
                elif form == 'marker-hardlink':
                    os.link(marker, outside/'marker-copy')
                elif form == 'bad-marker':
                    marker.write_text('{}')
                elif form == 'copied-marker':
                    other = root/'releases'/'.release-abcdefgh'
                    other.mkdir(mode=0o700)
                    shutil.copyfile(marker, other/'.installer-private.json')
                    (other/'.installer-private.json').chmod(0o600)
                    shutil.rmtree(stale)
                    stale = other
                real = Path.lstat
                def metadata(path, *args, **kwargs):
                    info = real(path, *args, **kwargs)
                    if path == stale and form == 'wrong-owner':
                        fields = list(info)
                        fields[4] = os.geteuid()+1
                        return os.stat_result(fields)
                    return info
                with patch.object(Path, 'lstat', metadata):
                    install.cleanup_private_releases(root, time.time_ns())
                self.assertTrue(stale.exists() or stale.is_symlink(), form)
                self.assertEqual((outside/'sentinel').read_bytes(), b'KEEP')

    def test_cleanup_failure_warns_but_keeps_success_and_tries_other_trees(self):
        import shutil
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = self.stage(directory)
            first = self.killed_install(root)
            other = self.killed_install(self.stage(Path(directory)/'other'))
            second = root/'releases'/other.name
            other.rename(second)
            real = shutil.rmtree
            def failed(path, *args, **kwargs):
                if Path(path) == first:
                    raise OSError('injected cleanup failure')
                return real(path, *args, **kwargs)
            with patch.object(install.shutil, 'rmtree', side_effect=failed), \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                result = install.install(root, ROOT)
            self.assertEqual(result['action'], 'installed')
            self.assertEqual(install.pointer(root), result['release'])
            self.assertTrue(first.exists())
            self.assertFalse(second.exists())
            self.assertIn(str(first), error.getvalue())

    def test_failed_install_does_not_clean_prior_private_trees(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = self.stage(directory)
            stale = self.killed_install(root)
            with patch.object(install, 'promote', side_effect=OSError('injected promotion failure')):
                with self.assertRaises(OSError):
                    install.install(root, ROOT)
            self.assertTrue(stale.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
