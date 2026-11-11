#!/usr/bin/env python3
"""Exercise setup suites through a symlinked TMPDIR, as on macOS /var."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TempPortabilityTests(unittest.TestCase):
    def test_setup_suites_pass_with_aliased_tmpdir(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            base = Path(os.path.realpath(directory))
            real = base/'real'
            real.mkdir()
            alias = base/'alias'
            alias.symlink_to(real, target_is_directory=True)
            env = dict(os.environ, TMPDIR=str(alias), TMP=str(alias), TEMP=str(alias))
            for suite in ('test_interview.py', 'test_operating_model_fixes.py'):
                with self.subTest(suite=suite):
                    run = subprocess.run([sys.executable, str(ROOT/'tests'/suite)],
                                         env=env, capture_output=True, text=True, timeout=60)
                    self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
                    self.assertIn('OK', run.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
