#!/usr/bin/env python3
"""Installer rehearsal only, using disposable source and target trees."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
class InstallTests(unittest.TestCase):
    def test_deployment_rejects_worker_owned_ancestors(self):
        from unittest.mock import patch
        sys.path.insert(0,str(ROOT/'deploy/one_door'))
        import install
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)/'stage'
            with patch.object(install.os,'geteuid',return_value=0):
                with self.assertRaises(ValueError): install.check_root(root,False)
            install.check_root(root,True)

    def test_real_shell_wrappers_install_repeat_and_restore(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            dest = Path(tmp)/'stage'
            def invoke(script,*args):
                result = subprocess.run(['bash',str(ROOT/'deploy'/script),'--root',str(dest),'--rehearsal',*args],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                return json.loads(result.stdout)
            first = invoke('install_one_door.sh')
            self.assertEqual(invoke('install_one_door.sh')['action'],'unchanged')
            self.assertEqual(invoke('restore_one_door.sh','restore','--backup',first['backup'])['action'],'restored')
            self.assertFalse((dest/'current').exists())

    def test_idempotent_backup_and_one_step_restore(self):
        script = ROOT/'deploy/one_door/install.py'
        self.assertTrue(script.is_file(), 'offline guarded installer missing')
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp = Path(tmp)
            kit = tmp/'kit'
            shutil.copytree(ROOT/'deploy/one_door',kit/'deploy/one_door')
            shutil.copytree(ROOT/'plugins/send-lock',kit/'plugins/send-lock', ignore=shutil.ignore_patterns('__pycache__'))
            dest = tmp/'staging'
            def run(*args, ok=True):
                r = subprocess.run([sys.executable,str(script),'--root',str(dest),'--rehearsal',*args],text=True,capture_output=True)
                if ok: self.assertEqual(r.returncode,0,r.stderr)
                else: self.assertNotEqual(r.returncode,0)
                return json.loads(r.stdout) if ok else None
            first = run('install','--kit',str(kit))
            current = (dest/'current').resolve()
            self.assertEqual(json.loads((current/'guard/axiom-owner.json').read_text())['step'],0)
            self.assertEqual((current/'guard/config.yaml').stat().st_mode & 0o777, 0o444)
            self.assertEqual((current/'guard/plugins/send-lock/__init__.py').stat().st_mode & 0o777, 0o444)
            self.assertEqual(run('install','--kit',str(kit))['action'],'unchanged')
            self.assertEqual(len(list((dest/'backups').iterdir())),1)
            # Explicit owner settings survive the staged upgrade, not reset to 0.
            owner_cfg = current/'guard/axiom-owner.json'
            owner_cfg.chmod(0o644)
            owner_cfg.write_text('{"step":2,"standing_permissions":[]}')
            owner_cfg.chmod(0o444)
            # Simulate a later local kit version, then restore exact prior pointer.
            with (kit/'plugins/send-lock/__init__.py').open('a') as f: f.write('\n# fake next version\n')
            second = run('install','--kit',str(kit))
            self.assertEqual(json.loads((dest/'current/guard/axiom-owner.json').read_text())['step'],2)
            self.assertNotEqual((dest/'current').resolve(),current)
            run('restore','--backup',second['backup'])
            self.assertEqual((dest/'current').resolve(),current)
            run('restore','--backup',first['backup'])
            self.assertFalse((dest/'current').exists())
            run('restore','--backup','../escape',ok=False)
            (dest/'current').symlink_to('/tmp/not-a-release')
            run('install','--kit',str(kit),ok=False)

if __name__ == '__main__': unittest.main(verbosity=2)
