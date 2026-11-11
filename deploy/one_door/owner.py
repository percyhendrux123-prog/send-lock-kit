"""Owner-side commands. No listening endpoint; never expose to the worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from sender import ID, read_regular, owner_config


def atomic(path, raw):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.owner-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def approve(root, filename, expected_hash):
    if not ID.fullmatch(filename):
        raise ValueError('invalid draft filename')
    digest = hashlib.sha256(read_regular(root/'outbox'/filename)).hexdigest()
    if digest != expected_hash:
        raise ValueError('draft changed since owner inspection')
    value = {'draft_id':filename[:-5], 'sha256':digest, 'approved':True}
    atomic(root/'approvals'/filename, json.dumps(value).encode())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=Path('/data'))
    sub = p.add_subparsers(dest='action', required=True)
    a = sub.add_parser('approve')
    a.add_argument('filename'); a.add_argument('--sha256',required=True)
    s = sub.add_parser('step'); s.add_argument('value',type=int,choices=range(5))
    args = p.parse_args()
    if args.action == 'approve':
        approve(args.root,args.filename,args.sha256)
    else:
        cfg = owner_config(args.root/'guard/axiom-owner.json')
        cfg['step'] = args.value
        atomic(args.root/'guard/axiom-owner.json',json.dumps(cfg).encode())

if __name__ == '__main__': main()
