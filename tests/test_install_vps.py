#!/usr/bin/env python3
"""Exercise the bootstrap with fake installer/OS commands, never changing this OS."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/install_vps.sh'


class InstallTests(unittest.TestCase):
    def test_shell_syntax_and_plan_have_no_side_effects(self):
        syntax = subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        plan = subprocess.run(['bash', str(SCRIPT), '--plan', '--non-interactive'], capture_output=True, text=True)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn('https://hermes-agent.nousresearch.com/install.sh', plan.stdout)
        self.assertIn('NOT started', plan.stdout)

    def test_invalid_arguments_fail_before_install(self):
        for args in (['--non-interactive', '--user', 'root'],
                     ['--non-interactive', '--ssh-port', '0'],
                     ['--non-interactive', '--user', '../x'], ['--unknown'], []):
            with self.subTest(args=args):
                run = subprocess.run(['bash', str(SCRIPT), *args], capture_output=True, text=True)
                self.assertEqual(run.returncode, 2, run.stderr)

    def test_mock_container_install_stages_plugin_and_inert_user_service(self):
        self._mock_install(container=True)

    def test_mock_vps_preserves_ssh_before_firewall_and_never_enables_gateway(self):
        self._mock_install(container=False)

    def test_low_ram_refused_before_any_machine_changes(self):
        self._mock_install(container=False, memory_kib=2*1024*1024)

    def test_container_memory_limit_is_also_checked(self):
        self._mock_install(container=True, memory_limit=2*1024*1024*1024)

    def test_unknown_ram_fails_closed(self):
        self._mock_install(container=True, memory_kib=0)

    def test_nested_cgroup_cap_is_read_along_with_ancestors(self):
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                root = Path(directory)
                mount = root/'cgroup' if version == 2 else root/'cgroup/memory'
                group = mount/'user.slice/session.scope'
                group.mkdir(parents=True)
                name = 'memory.max' if version == 2 else 'memory.limit_in_bytes'
                unlimited = 'max' if version == 2 else '9223372036854771712'
                (mount/name).write_text(unlimited+'\n')
                (group.parent/name).write_text('1999000123\n')
                (group/name).write_text(unlimited+'\n')
                membership = root/'self-cgroup'
                membership.write_text(('0::' if version == 2 else '5:memory:')+'/user.slice/session.scope\n')
                command = 'source "$1"; memory_limit_bytes "$2" "$3"'
                run = subprocess.run(['bash', '-c', command, '_', str(SCRIPT), str(membership), str(root/'cgroup')], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout.strip(), '1999000123')

    def test_zero_cgroup_cap_also_refuses(self):
        self._mock_install(container=True, memory_limit=0)

    def _mock_install(self, container, memory_kib=6*1024*1024, memory_limit='max'):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            temp = Path(directory)
            home = temp / 'home'
            home.mkdir()
            fakebin = temp / 'bin'
            fakebin.mkdir()
            calls = temp / 'calls.txt'
            installer = temp / 'official-installer.sh'
            installer.write_text('''#!/bin/bash
set -eu
printf 'installer %s\\n' "$*" >> "$HOME/calls.txt"
mkdir -p "$HOME/.local/bin"
printf '#!/bin/bash\\nprintf "hermes %%s\\\\n" "$*" >> "$HOME/calls.txt"\\n' > "$HOME/.local/bin/hermes"
chmod +x "$HOME/.local/bin/hermes"
''')
            # Every machine-changing command is a test-local stub. runuser is
            # replaced by executing only its argv (the production clean env).
            stubs = {
                'id': '#!/bin/bash\nif [ "$1" = -u ]; then printf "1001\\n"; else test -f "$FAKE_HOME/.user-created"; fi\n',
                'useradd': '#!/bin/bash\nprintf "useradd %s\\n" "$*" >> "$FAKE_CALLS"; touch "$FAKE_HOME/.user-created"\n',
                'getent': '#!/bin/bash\nprintf "kitbot:x:1001:1001::%s:/bin/bash\\n" "$FAKE_HOME"\n',
                'apt-get': '#!/bin/bash\nprintf "apt %s\\n" "$*" >> "$FAKE_CALLS"\n',
                'chown': '#!/bin/bash\nexit 0\n',
                'curl': '#!/bin/bash\nwhile [ "$1" != "-o" ]; do shift; done; cp "$FAKE_INSTALLER" "$2"\n',
                'runuser': '#!/bin/bash\nif [[ $* == *systemctl* ]]; then printf "user-service %s\\n" "$*" >> "$FAKE_CALLS"; exit 0; fi\nwhile [ "$1" != "--" ]; do shift; done; shift; exec "$@"\n',
                'ufw': '#!/bin/bash\nprintf "ufw %s\\n" "$*" >> "$FAKE_CALLS"\n',
                'loginctl': '#!/bin/bash\nprintf "loginctl %s\\n" "$*" >> "$FAKE_CALLS"\n',
                'systemctl': '#!/bin/bash\nprintf "systemctl %s\\n" "$*" >> "$FAKE_CALLS"\n',
            }
            for name, content in stubs.items():
                path = fakebin / name
                path.write_text(content)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fakebin) + ':' + os.environ['PATH'],
                       FAKE_HOME=str(home), FAKE_CALLS=str(calls), FAKE_INSTALLER=str(installer), TMPDIR=str(temp))
            # Override ONLY the root/OS probes, not the install implementation.
            command = ('source "$1"; require_root() { :; }; require_ubuntu() { :; }; '
                       f'memory_total_kib() {{ printf "%s\\n" {memory_kib}; }}; '
                       f'memory_limit_bytes() {{ printf "%s\\n" {memory_limit}; }}; '
                       'main --non-interactive --user kitbot ' + ('--container' if container else '--ssh-port 2222'))
            if memory_kib == 0 or memory_kib*1024 < 6000000000 or (memory_limit != 'max' and memory_limit < 6000000000):
                low = subprocess.run(['bash', '-c', command, '_', str(SCRIPT)], env=env, capture_output=True, text=True)
                self.assertEqual(low.returncode, 2, low.stdout+low.stderr)
                self.assertIn('6 GB', low.stderr)
                self.assertFalse(calls.exists(), 'RAM preflight must precede apt/user/firewall changes')
                self.assertFalse((home/'.user-created').exists())
                self.assertFalse((home/'calls.txt').exists())
                return
            (home / '.user-created').touch()
            preexisting = subprocess.run(['bash', '-c', command, '_', str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(preexisting.returncode, 2, preexisting.stdout + preexisting.stderr)
            self.assertFalse(calls.exists(), 'Existing account must be refused before apt/firewall changes')
            (home / '.user-created').unlink()
            run = subprocess.run(['bash', '-c', command, '_', str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('NOT started', run.stdout)
            self.assertEqual((home / '.hermes/plugins/send-lock/__init__.py').read_bytes(),
                             (ROOT / 'plugins/send-lock/__init__.py').read_bytes())
            self.assertIn('linux', (home / '.hermes/plugins/send-lock/plugin.yaml').read_text())
            unit = (home / '.config/systemd/user/hermes-gateway.service').read_text()
            self.assertIn('ExecStart=%h/.local/bin/hermes gateway', unit)
            self.assertIn('ConditionPathExists=%h/.hermes/AXIOM-GATEWAY-APPROVED', unit)
            recorded = (home / 'calls.txt').read_text()
            self.assertIn('--non-interactive', recorded)
            self.assertIn('plugins enable send-lock', recorded)
            for absent in ('gateway start', 'gateway setup', 'model', 'login'):
                self.assertNotIn(absent, recorded)
            for secret in ('.env', 'auth.json', 'axiom-owner.json'):
                self.assertFalse((home / '.hermes' / secret).exists())
            machine_calls = calls.read_text()
            if container:
                for absent in ('ufw ', 'loginctl ', 'systemctl '):
                    self.assertFalse(any(line.startswith(absent) for line in machine_calls.splitlines()))
            else:
                self.assertLess(machine_calls.index('ufw allow 2222/tcp'), machine_calls.index('ufw --force enable'))
                self.assertIn('loginctl enable-linger kitbot', machine_calls)
                self.assertIn('systemctl --user daemon-reload', machine_calls)
                self.assertNotIn('enable --now', machine_calls)
                self.assertNotIn('start hermes-gateway', machine_calls)
            # Guard against accidental second invocation/upgrades, before a download.
            repeat = subprocess.run(['bash', '-c', command, '_', str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(repeat.returncode, 2)
            self.assertIn('existing setup', repeat.stderr)
            self.assertEqual((home / 'calls.txt').read_text(), recorded)
            # A different existing Hermes home must not be adopted or updated.
            import shutil
            shutil.rmtree(home / '.hermes/plugins/send-lock')
            (home / '.config/systemd/user/hermes-gateway.service').unlink()
            adopted = subprocess.run(['bash', '-c', command, '_', str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(adopted.returncode, 2)
            self.assertEqual((home / 'calls.txt').read_text(), recorded)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    failures = len(result.failures) + len(result.errors)
    print('install_vps (mock OS): %d passed, %d failed' % (result.testsRun - failures, failures))
    sys.exit(0 if result.wasSuccessful() else 1)
