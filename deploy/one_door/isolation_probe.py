#!/usr/bin/env python3
"""Isolation probe (plan 3b): run inside a throwaway probe container on ONE network.

Tries each target once and reports whether it was REACHED. Every attempt is a
bare TCP connect (nothing is sent) or a DNS lookup; no data leaves even when a
connect succeeds. The host side evaluates the rows: every target must fail
except the explicitly allowed (network, target) pairs, e.g. the proxy's route.
Each network also probes one service known to listen on it (a positive control),
which must be reached: a probe that cannot connect to anything proves nothing.
A blocked target with no listener behind it shows only that nothing answered;
the host firewall's packet log is the evidence that it was dropped.

  probe:     isolation_probe.py probe --network worker-net --targets '<json list>'
  evaluate:  isolation_probe.py evaluate --allow egress-net:api results.jsonl ...

A target is {"name": str, "kind": "tcp"|"dns", "host": str, "port": int}.
"""
import argparse
import json
import socket
import sys
import time


def attempt(target, timeout=3.0):
    kind, host = target['kind'], target['host']
    try:
        if kind == 'dns':
            socket.getaddrinfo(host, None)
            return True, 'resolved'
        family = socket.AF_INET6 if ':' in host else socket.AF_UNSPEC
        infos = socket.getaddrinfo(host, target['port'], family, socket.SOCK_STREAM)
        last = 'no address'
        for fam, typ, proto, _, addr in infos:
            s = socket.socket(fam, typ, proto)
            s.settimeout(timeout)
            try:
                s.connect(addr)
                return True, 'connected'
            except OSError as error:
                last = type(error).__name__ + (':%s' % error.errno if getattr(error, 'errno', None) else '')
            finally:
                s.close()
        return False, last
    except OSError as error:
        return False, type(error).__name__ + (':%s' % error.errno if getattr(error, 'errno', None) else '')


def probe(network, targets):
    rows = []
    for target in targets:
        reached, detail = attempt(target)
        rows.append({'t': time.time(), 'network': network, 'name': target['name'], 'kind': target['kind'],
                     'host': target['host'], 'port': target.get('port'), 'reached': reached, 'detail': detail})
    return rows


def evaluate(rows, allowed):
    """Return (problems, summary). Allowed pairs MUST be reached; everything else must fail."""
    problems = []
    seen = set()
    for row in rows:
        pair = '%s:%s' % (row['network'], row['name'])
        seen.add(pair)
        if pair in allowed and not row['reached']:
            problems.append('%s: the one permitted route was NOT reached (%s)' % (pair, row['detail']))
        if pair not in allowed and row['reached']:
            problems.append('%s: REACHED %s %s:%s (%s)' % (pair, row['kind'], row['host'], row.get('port'), row['detail']))
    for pair in sorted(set(allowed) - seen):
        problems.append('%s: permitted route was not probed' % pair)
    return problems, {'rows': len(rows), 'reached': sum(1 for r in rows if r['reached'])}


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('probe')
    a.add_argument('--network', required=True)
    a.add_argument('--targets', required=True)
    b = sub.add_parser('evaluate')
    b.add_argument('--allow', action='append', default=[])
    b.add_argument('--expect-networks', default='')
    b.add_argument('--expect-targets', default='')
    b.add_argument('results', nargs='+')
    args = p.parse_args(argv)
    if args.cmd == 'probe':
        for row in probe(args.network, json.loads(args.targets)):
            print(json.dumps(row, separators=(',', ':')), flush=True)
        return 0
    rows = []
    for path in args.results:
        with open(path, encoding='utf-8') as f:
            rows += [json.loads(line) for line in f if line.strip()]
    problems, summary = evaluate(rows, set(args.allow))
    want = {n for n in args.expect_networks.split(',') if n}
    missing = want - {r['network'] for r in rows}
    problems += ['network %s was not probed' % n for n in sorted(missing)]
    # The full matrix: every expected target on every expected network, so a missing negative
    # probe cannot pass as isolation.
    targets = {t for t in args.expect_targets.split(',') if t}
    seen = {(r['network'], r['name']) for r in rows}
    problems += ['%s:%s was not probed' % (n, t) for n in sorted(want) for t in sorted(targets) if (n, t) not in seen]
    for row in rows:
        print('%-12s %-14s %-4s %-28s %s' % (row['network'], row['name'], row['kind'],
                                            '%s:%s' % (row['host'], row.get('port') or ''),
                                            ('REACHED ' if row['reached'] else 'blocked ') + row['detail']))
    for problem in problems:
        print('ISOLATION PROBLEM: ' + problem)
    print('isolation: %s (%d attempts, %d reached, %d permitted)' % (
        'PASS' if not problems else 'FAIL', summary['rows'], summary['reached'], len(args.allow)))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
