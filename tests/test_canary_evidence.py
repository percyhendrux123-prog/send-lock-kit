#!/usr/bin/env python3
"""Unit tests over independently supplied synthetic observer records."""
from base64 import b64encode
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'deploy/one_door'))
from canary_evidence import Canary, Observation, check_baselines, scan


OBSERVERS = ('receiver', 'tripwire', 'sender-journal', 'owner', 'approval', 'packet-log')


def reachable():
    return Canary.mint('reachable', 'worker', 100, 200,
                       permitted={'transcript', 'session'}, forbidden=set(OBSERVERS))


class CanaryEvidenceTests(unittest.TestCase):
    def test_registered_origin_permitted_copies_destinations_and_half_open_window(self):
        reg = reachable()
        observations = [Observation(source, when, reg.value.encode()) for source, when in (
            ('worker', 100), ('transcript', 120), ('session', 150), ('receiver', 99),
            ('receiver', 100), ('tripwire', 199), ('receiver', 200), ('not-in-scope', 150))]
        arrivals = scan([reg], observations)
        self.assertEqual([(x.canary_id, x.observer, x.at, x.encoding) for x in arrivals],
                         [('reachable', 'receiver', 100, 'literal'),
                          ('reachable', 'tripwire', 199, 'literal')])
        self.assertNotIn(reg.value, repr(reg))
        self.assertNotIn(reg.value, repr(observations))
        self.assertNotIn(reg.value, repr(arrivals))

    def test_sealed_value_is_forbidden_at_every_observed_non_origin(self):
        reg = Canary.mint('sealed', 'owner', 100, 200)
        rows = [Observation(name, 150, reg.value.encode()) for name in ('owner', 'worker', 'transcript')]
        self.assertEqual([x.observer for x in scan([reg], rows)], ['worker', 'transcript'])

    def test_hex_base64_and_percent_encoding_are_detected(self):
        reg = Canary.mint('sealed', 'owner', 100, 200)
        raw = reg.value.encode()
        forms = {'hex': raw.hex().upper().encode(),
                 'base64': b64encode(raw).rstrip(b'='),
                 'url': b'%53' + raw[1:10] + b''.join(f'%{n:02x}'.encode() for n in raw[10:])}
        for encoding, body in forms.items():
            with self.subTest(encoding=encoding):
                hits = scan([reg], [Observation('receiver', 150, b'prefix:' + body + b';suffix')])
                self.assertEqual([x.encoding for x in hits], [encoding])
        self.assertEqual(scan([reg], [Observation('receiver', 150, raw[:-1])]), ())

    def test_registry_keeps_tokens_synthetic_and_disallows_conflicting_policy(self):
        reg = reachable()
        self.assertRegex(reg.value, r'^SLKCANARY-[0-9a-f]{32}$')
        self.assertNotEqual(reg.value, reachable().value)
        for kwargs in ({'permitted': {'same'}, 'forbidden': {'same'}},
                       {'forbidden': {'worker'}}, {'permitted': {'copy'}},
                       {'forbidden': set()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Canary.mint('test', 'worker', 100, 200, **kwargs)
        for start, end in ((200, 100), (100, 100), (100, float('inf')), (True, 100)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                Canary.mint('test', 'worker', start, end)
        with self.assertRaises(ValueError):
            scan([reg, reachable()], [])
        with self.assertRaises(ValueError):
            Observation('receiver', 150, None)

    def baseline_inputs(self):
        reg = reachable()
        quiet = [Observation(source, 150, b'baseline: observed but no data sent')
                 for source in OBSERVERS]
        quiet += [Observation('worker', 150, reg.value.encode())]
        positives = {}
        for source in OBSERVERS:
            planted = Canary.mint(f'positive-{source}', 'control-source', 100, 200)
            positives[source] = (planted, [Observation(source, 150, planted.value.encode())])
        return reg, quiet, positives

    def test_baselines_require_observed_quiet_windows_and_every_synthetic_positive(self):
        reg, quiet, positives = self.baseline_inputs()
        report = check_baselines(OBSERVERS, [reg], quiet, positives)
        self.assertTrue(report.passed)
        leaks = quiet + [Observation('owner', 150, reg.value.encode())]
        quiet_missing = [row for row in leaks if row.observer != 'packet-log']
        positive, _ = positives['receiver']
        positives['receiver'] = (positive, [Observation('receiver', 200, positive.value.encode())])
        positives.pop('approval')
        report = check_baselines(OBSERVERS, [reg], quiet_missing, positives)
        self.assertFalse(report.passed)
        self.assertEqual(report.missing_no_action, ('packet-log',))
        self.assertEqual([x.observer for x in report.unauthorized_no_action], ['owner'])
        self.assertEqual(report.failed_positives, ('approval', 'receiver'))

    def test_baselines_reject_reused_values_and_no_active_registry(self):
        reg, quiet, positives = self.baseline_inputs()
        report = check_baselines(OBSERVERS, [], quiet, positives)
        self.assertFalse(report.passed)
        self.assertEqual(report.missing_no_action, tuple(sorted(OBSERVERS)))
        positives['receiver'] = positives['tripwire']
        with self.assertRaises(ValueError):
            check_baselines(OBSERVERS, [reg], quiet, positives)

    def test_no_action_baseline_requires_the_origin_was_actually_planted(self):
        reg, quiet, positives = self.baseline_inputs()
        report = check_baselines(OBSERVERS, [reg], [row for row in quiet if row.observer != 'worker'], positives)
        self.assertFalse(report.passed)
        self.assertEqual(report.unseen_origins, (reg.canary_id,))

    def test_positive_cannot_pass_by_omitting_a_cross_observer_arrival_from_policy(self):
        reg, quiet, positives = self.baseline_inputs()
        planted = Canary.mint('too-narrow', 'control-source', 100, 200, forbidden={'receiver'})
        positives['receiver'] = (planted, [Observation(source, 150, planted.value.encode())
                                           for source in ('receiver', 'tripwire')])
        report = check_baselines(OBSERVERS, [reg], quiet, positives)
        self.assertFalse(report.passed)
        self.assertEqual(report.failed_positives, ('receiver',))


if __name__ == '__main__':
    unittest.main(verbosity=2)
