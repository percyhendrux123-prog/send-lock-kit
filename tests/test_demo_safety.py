#!/usr/bin/env python3
"""Wrapper lifecycle tests with fake binaries; never invokes Docker or Colima."""
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DemoSafetyTests(unittest.TestCase):
    def test_context_allowlist_rejects_unknown_files_in_source_trees(self):
        rules = (ROOT / '.dockerignore').read_text().splitlines()
        def included(path):
            allowed = True
            for rule in rules:
                if not rule or rule.startswith('#'):
                    continue
                negative = rule.startswith('!')
                pattern = rule.lstrip('!').rstrip('/')
                # Docker applies a matching directory rule to descendants too.
                # This is an offline approximation, not an engine execution.
                candidates = [path] + [str(p) for p in Path(path).parents if str(p) != '.']
                if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
                    allowed = negative
            return allowed
        for path in ('scripts/google_client_secret.json', 'scripts/.netrc', 'deploy/id_ed25519',
                     'scripts/future-unreviewed.py', 'demo/fixtures/new-credentials.json'):
            self.assertFalse(included(path), path)
        for path in ('.dockerignore', 'scripts/rundown.py', 'scripts/stall_watch.py', 'deploy/install_vps.sh',
                     'demo/fixtures/mail.json', 'tests/test_demo.py'):
            self.assertTrue(included(path), path)
        import shlex
        dockerfile = (ROOT / 'demo/Dockerfile').read_text()
        copied_sources = []
        for line in dockerfile.splitlines():
            if line.startswith('COPY '):
                parts = [p for p in shlex.split(line)[1:] if not p.startswith('--')]
                copied_sources.extend(parts[:-1])
        self.assertIn('.dockerignore', copied_sources, 'Container safety tests need the reviewed context rules')
        for source in copied_sources:
            self.assertTrue((ROOT / source).is_file(), 'Only explicit source files, not whole directories: ' + source)
            self.assertTrue(included(source), source)

    def _wrapper(self, *, listing='[]', list_rc=0, start_rc=0, run_rc=0):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            temp = Path(directory)
            (temp / 'demo').mkdir()
            shutil.copy(ROOT / 'demo/run.sh', temp / 'demo/run.sh')
            fakebin = temp / 'bin'
            fakebin.mkdir()
            binaries = {
                'uname': '#!/bin/bash\nprintf "Darwin\\n"\n',
                'docker': '''#!/bin/bash
printf 'docker %s\\n' "$*" >> "$MOCK_DIR/calls"
case "$1" in
info) test -f "$MOCK_DIR/started" ;;
build) exit 0 ;;
run) printf 'SYNTHETIC WRAPPER TEST ONLY\\n'; exit "$MOCK_RUN_RC" ;;
*) exit 9 ;;
esac
''',
                'colima': '''#!/bin/bash
printf 'colima %s\\n' "$*" >> "$MOCK_DIR/calls"
case "$1" in
list) printf '%s\\n' "$MOCK_LISTING"; exit "$MOCK_LIST_RC" ;;
status) exit "$MOCK_LIST_RC" ;;
start) if [ "$MOCK_START_RC" = 0 ]; then touch "$MOCK_DIR/started"; fi; exit "$MOCK_START_RC" ;;
stop) rm -f "$MOCK_DIR/started" ;;
*) exit 9 ;;
esac
'''}
            for name, content in binaries.items():
                path = fakebin / name
                path.write_text(content)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fakebin) + ':' + os.environ['PATH'], MOCK_DIR=str(temp),
                       MOCK_LISTING=listing, MOCK_LIST_RC=str(list_rc), MOCK_START_RC=str(start_rc), MOCK_RUN_RC=str(run_rc))
            result = subprocess.run(['bash', str(temp / 'demo/run.sh')], capture_output=True, text=True, env=env)
            calls = (temp / 'calls').read_text()
            output = temp / 'demo/OUTPUT.txt'
            return result.returncode, calls, output.exists()

    def test_unknown_or_running_colima_is_never_started_or_stopped(self):
        for args in ({'list_rc': 2}, {'listing': 'not-json'},
                     {'listing': '{"name":"default","status":"Running"}'},
                     {'listing': '{"name":"default","status":"Unknown"}'}):
            rc, calls, output = self._wrapper(**args)
            self.assertNotEqual(rc, 0)
            self.assertNotIn('colima start', calls)
            self.assertNotIn('colima stop', calls)
            self.assertFalse(output)

    def test_failed_start_does_not_claim_daemon_ownership(self):
        rc, calls, output = self._wrapper(start_rc=2)
        self.assertNotEqual(rc, 0)
        self.assertIn('colima start', calls)
        self.assertNotIn('colima stop', calls)
        self.assertFalse(output)

    def test_started_here_is_stopped_after_success_or_runtime_failure(self):
        for run_rc in (0, 7):
            rc, calls, output = self._wrapper(run_rc=run_rc)
            self.assertEqual(rc, run_rc)
            self.assertIn('colima start', calls)
            self.assertIn('colima stop', calls)
            self.assertEqual(output, run_rc == 0)
            self.assertIn('--network none', calls)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    failures = len(result.failures) + len(result.errors)
    print('demo safety (mock binaries): %d passed, %d failed' % (result.testsRun - failures, failures))
    sys.exit(0 if result.wasSuccessful() else 1)
