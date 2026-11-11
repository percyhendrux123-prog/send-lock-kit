#!/usr/bin/env python3
"""Offline structural checks, NOT evidence of Docker network enforcement."""
import fnmatch
import json
from pathlib import Path
import shlex
import unittest

ROOT = Path(__file__).resolve().parents[1]
class LayoutTests(unittest.TestCase):
    def test_worker_boundary_and_separate_authorities(self):
        cfg = json.loads((ROOT/'deploy/one_door/compose.json').read_text())
        svc = cfg['services']; worker = svc['worker']
        self.assertEqual(worker['networks'], ['worker-net'])
        self.assertEqual(worker['cap_drop'], ['ALL'])
        self.assertEqual(worker['dns'], ['127.0.0.1'])
        self.assertEqual(cfg['networks']['worker-net']['driver_opts']['com.docker.network.bridge.gateway_mode_ipv4'],'isolated')
        self.assertNotIn('HTTP_PROXY',worker['environment'])
        self.assertNotIn('HTTPS_PROXY',worker['environment'])
        self.assertTrue(worker['read_only'])
        self.assertIn('no-new-privileges:true',worker['security_opt'])
        self.assertNotEqual(worker['user'],svc['owner']['user'])
        self.assertNotEqual(worker['user'],svc['sender']['user'])
        self.assertIn('approvals:/approvals:ro',worker['volumes'])
        self.assertIn('guard:/guard:ro',worker['volumes'])
        self.assertFalse(any('secret' in v or 'sock' in v for v in worker['volumes']))
        self.assertIn('send-secret:/data/secrets:ro',svc['sender']['volumes'])
        self.assertEqual(svc['owner']['network_mode'],'none')
        self.assertTrue(all(n['internal'] for n in cfg['networks'].values()))
        for name,s in svc.items():
            self.assertNotIn('ports',s,name)
            self.assertNotIn('privileged',s,name)
            self.assertFalse(any('docker.sock' in v for v in s.get('volumes',[])))
        self.assertEqual(set(svc['egress-gateway']['networks']),{'worker-net','outside-net'})
        self.assertEqual(svc['fake-outside']['networks'],['outside-net'])

    def test_only_fake_destinations_and_practice_default(self):
        d = ROOT/'deploy/one_door'
        self.assertEqual(json.loads((d/'axiom-owner.json').read_text())['step'],0)
        for route in json.loads((d/'egress.json').read_text()).values():
            self.assertEqual(route['origin'],'http://fake-api:8080')
        hermes = (d/'config.yaml').read_text()
        for value in ['mcp_servers: {}','enabled: [send-lock]',
                      'browser, computer, phone, delegation, code_execution',
                      'cli: [terminal, file, skills, todo, tts]']:
            self.assertIn(value,hermes)

    def test_image_copy_and_context_are_explicit(self):
        rules = (ROOT/'.dockerignore').read_text().splitlines()
        def included(path):
            allowed = True
            for rule in rules:
                if not rule or rule.startswith('#'): continue
                candidates = [path]+[str(p) for p in Path(path).parents if str(p) != '.']
                if any(fnmatch.fnmatchcase(c,rule.lstrip('!').rstrip('/')) for c in candidates):
                    allowed = rule.startswith('!')
            return allowed
        for line in (ROOT/'deploy/one_door/Dockerfile').read_text().splitlines():
            if line.startswith('COPY '):
                for source in shlex.split(line)[1:-1]:
                    self.assertTrue((ROOT/source).is_file(),source)
                    self.assertTrue(included(source),source)
        for path in ['deploy/one_door/real-token.json','deploy/one_door/.env',
                     'deploy/one_door/current/guard/axiom-owner.json','deploy/one_door/new.py']:
            self.assertFalse(included(path),path)

if __name__ == '__main__': unittest.main(verbosity=2)
