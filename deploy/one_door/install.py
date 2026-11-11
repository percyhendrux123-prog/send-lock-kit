"""Offline staging installer; never starts/stops a gateway or reads a client home.

Root owns the staging tree (a different Unix user from worker UID 10001). The
--rehearsal path intentionally uses the caller's UID, not a security deployment.
Only the `current` symlink changes; dated backup records give a one-command undo.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from owner import atomic
from sender import read_regular, load

FILES = ['sender.py','gateway.py','owner.py','config.yaml','axiom-owner.json','egress.json']
RELEASE_FILES = [('guard/' if name in {'config.yaml','axiom-owner.json','egress.json'} else 'code/')+name for name in FILES] + ['guard/plugins/send-lock/__init__.py', 'guard/plugins/send-lock/plugin.yaml']
PRIVATE_MARKER = '.installer-private.json'


def mark_private_release(path, started_ns):
    info = path.lstat()
    record = {'installer':'axiom-one-door-private-v1', 'name':path.name,
              'device':info.st_dev, 'inode':info.st_ino, 'created_ns':started_ns}
    fd = os.open(path/PRIVATE_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(record, file)
        file.flush()
        os.fsync(file.fileno())
    sync_directory(path)


def cleanup_private_releases(root, started_ns):
    # No name-only deletion. Old unmarked trees cannot be identified safely.
    # Require fd-based rmtree so a replaced descendant symlink is never followed.
    if not shutil.rmtree.avoids_symlink_attacks:
        return
    try:
        check_root(root, rehearsal=True)
        check_node(root, directory=True)
        check_node(root/'releases', directory=True)
        paths = list((root/'releases').iterdir())
    except (OSError, ValueError):
        print('Private release cleanup skipped: unsafe or unavailable staging folder '+str(root), file=sys.stderr)
        return
    for path in paths:
        if not re.fullmatch(r'\.release-[a-z0-9_]{8}', path.name):
            continue
        try:
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or info.st_mtime_ns >= started_ns):
                continue
            check_tree(path)  # No links, foreign owners or shared writable nodes.
            # A valid marker is not permission to erase added owner files.
            directories = {str(parent) for name in RELEASE_FILES for parent in Path(name).parents}
            known = True
            for child in path.rglob('*'):
                relative = child.relative_to(path)
                if stat.S_ISDIR(child.lstat().st_mode):
                    known = str(relative) in directories
                else:
                    known = (str(relative) in [*RELEASE_FILES, PRIVATE_MARKER] or
                             str(relative.parent) in directories and
                             re.fullmatch(r'\.owner-[a-z0-9_]{8}', relative.name) is not None)
                if not known:
                    break
            if not known:
                continue
            marker = path/PRIVATE_MARKER
            check_node(marker)
            if stat.S_IMODE(marker.lstat().st_mode) != 0o600:
                continue
            record = load(read_regular(marker))
            if not isinstance(record, dict):
                continue
            created = record.get('created_ns')
            if type(created) is not int or not 0 < created < started_ns:
                continue
            if record != {'installer':'axiom-one-door-private-v1', 'name':path.name,
                          'device':info.st_dev, 'inode':info.st_ino, 'created_ns':created}:
                continue
        except (OSError, ValueError, UnicodeError):
            continue  # Not a verifiable private tree: leave it for the owner.
        try:
            shutil.rmtree(path)
            sync_directory(root/'releases')
        except OSError:
            print('Private release cleanup incomplete: '+str(path), file=sys.stderr)


def check_node(path, directory=False, readonly=False):
    info = path.lstat()  # Never follow a trusted descendant symlink.
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    reason = None
    if not valid_type:
        reason = 'symlink refused' if stat.S_ISLNK(info.st_mode) else 'not a '+('directory' if directory else 'regular file')
    elif info.st_uid != os.geteuid():
        reason = 'belongs to another user'
    elif info.st_mode & 0o022:
        reason = 'group/world writable'
    elif not directory and info.st_nlink != 1:
        reason = 'hardlinked file refused'
    elif not directory and readonly and info.st_mode & 0o222:
        reason = 'release file is writable'
    if reason:
        raise ValueError('unsafe staging descendant: '+str(path)+' ('+reason+')')


def check_tree(path, readonly=False):
    check_node(path, directory=True)
    for child in path.iterdir():
        if stat.S_ISDIR(child.lstat().st_mode):
            check_tree(child, readonly)
        else:
            check_node(child, readonly=readonly)


def check_staging(root):
    check_node(root, directory=True)
    for name in ('releases', 'backups'):
        check_tree(root/name)


def check_release(root, link):
    if not isinstance(link, str) or not re.fullmatch(r'releases/[a-f0-9]{64}', link):
        raise ValueError('unmanaged release pointer')
    check_tree(root/link, readonly=True)
    for name in RELEASE_FILES:
        check_node(root/link/name, readonly=True)


def pointer(root):
    check_staging(root)
    p = root/'current'
    if not p.is_symlink():
        if p.exists(): raise ValueError('current is not a managed symlink')
        return None
    link = os.readlink(p)
    check_release(root, link)
    return link


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def promote(root, previous):
    old = pointer(root)  # Also validate when restoring to an absent pointer.
    if previous is not None:
        check_release(root, previous)
    def switch(link):
        p = root/'current'
        if link is None:
            p.unlink(missing_ok=True)
        else:
            temp = root/'.current-next'
            temp.unlink(missing_ok=True)
            temp.symlink_to(link)
            os.replace(temp,p)
    switch(previous)
    try:
        sync_directory(root)
    except OSError:
        # A reported failure must not leave a different current visible. If
        # storage remains broken, restoration durability still needs inspection.
        switch(old)
        sync_directory(root)
        raise


def install(root, kit):
    started_ns = time.time_ns()
    old = pointer(root)
    payload = {}
    for name in FILES:
        target = 'guard/'+name if name in {'config.yaml','axiom-owner.json','egress.json'} else 'code/'+name
        payload[target] = read_regular(kit/'deploy/one_door'/name)
    # Preserve explicit owner settings across upgrades; never reset a raised step.
    if old:
        for name in ['axiom-owner.json','egress.json']:
            payload['guard/'+name] = read_regular(root/old/'guard'/name)
    for name in ['__init__.py','plugin.yaml']:
        payload['guard/plugins/send-lock/'+name] = read_regular(kit/'plugins/send-lock'/name)
    fingerprint = hashlib.sha256(b''.join(k.encode()+b'\0'+v+b'\0' for k,v in sorted(payload.items()))).hexdigest()
    link = 'releases/'+fingerprint
    if old == link:
        # Re-check bytes and permissions, not just a marker or same version name.
        for name, content in payload.items():
            p = root/link/name
            if read_regular(p) != content or p.stat().st_mode & 0o222:
                raise ValueError('existing guard release modified; inspect before upgrade')
        cleanup_private_releases(root, started_ns)
        return {'action':'unchanged','release':link}
    release = root/link
    if release.exists():
        check_release(root, link)
        for name, content in payload.items():
            if read_regular(release/name) != content:
                raise ValueError('release digest collision or tampering')
    else:
        # Only complete releases get their final fingerprint pathname. A failed
        # write/chmod leaves current untouched and an identical retry can start
        # afresh; a killed process may leave an unused private .release-* tree.
        with tempfile.TemporaryDirectory(dir=root/'releases', prefix='.release-') as directory:
            staging = Path(directory)
            mark_private_release(staging, started_ns)
            for name, content in payload.items():
                path = staging/name
                path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
                atomic(path, content)
                path.chmod(0o444)
                with path.open('rb') as file:
                    os.fsync(file.fileno())
            # Marker is private recovery metadata, never a published payload.
            (staging/PRIVATE_MARKER).unlink()
            check_tree(staging, readonly=True)
            staging.chmod(0o755)
            for directory in sorted([staging, *[p for p in staging.rglob('*') if p.is_dir()]], key=lambda p: len(p.parts), reverse=True):
                sync_directory(directory)
            os.rename(staging, release)
            sync_directory(root/'releases')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = stamp+'.json'
    atomic(root/'backups'/backup, json.dumps({'previous':old,'installed':link}).encode())
    promote(root,link)
    cleanup_private_releases(root, started_ns)
    return {'action':'installed','release':link,'backup':backup}


def check_root(root, rehearsal):
    # Parent ownership matters: an untrusted writable parent can rename a
    # root-owned guard tree and substitute a new tree before the next mount.
    for candidate in [root,*root.parents]:
        if candidate.is_symlink():
            raise ValueError('symlinked staging ancestor refused: '+str(candidate))
        if not rehearsal and candidate.exists():
            info = candidate.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('deployment ancestor must be root-owned and not group/world writable: '+str(candidate))


def execute(args):
    if not args.rehearsal and os.geteuid() != 0:
        raise ValueError('root required for guard ownership; use --rehearsal only in a disposable folder')
    root = args.root.absolute()
    check_root(root,args.rehearsal)
    # Refuse symlink ancestors; never install into an agent-owned tree on host.
    if root == Path('/') or any(x.is_symlink() for x in [root,*root.parents]):
        raise ValueError('unsafe staging root directory')
    if root.exists() and (root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o022):
        raise ValueError('staging root must be owner-owned and not group/world writable')
    root.mkdir(parents=True,exist_ok=True,mode=0o755)
    for name in ['backups','releases']:
        d = root/name
        if d.exists() or d.is_symlink(): check_node(d, directory=True)
        d.mkdir(exist_ok=True,mode=0o755)
    check_staging(root)
    lock_path = root/'.install.lock'
    if lock_path.exists() or lock_path.is_symlink(): check_node(lock_path)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'install':
            result = install(root,args.kit)
        else:
            if not re.fullmatch(r'[0-9TZ.]+\.json',args.backup): raise ValueError('invalid backup name')
            record = load(read_regular(root/'backups'/args.backup))
            current = pointer(root)
            if current not in {record['installed'],record['previous']}:
                raise ValueError('restore would overwrite a different release; inspect first')
            previous = record['previous']
            if previous is not None and not re.fullmatch(r'releases/[a-f0-9]{64}',previous):
                raise ValueError('invalid previous release')
            promote(root,previous)
            result = {'action':'restored','release':previous}
    print(json.dumps(result))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--rehearsal',action='store_true')
    sub = p.add_subparsers(dest='action',required=True)
    a = sub.add_parser('install'); a.add_argument('--kit',type=Path,required=True)
    b = sub.add_parser('restore'); b.add_argument('--backup',required=True)
    args = p.parse_args()
    try:
        execute(args)
    except (OSError, ValueError) as error:
        # One plain line, including the selected folder and offending descendant.
        message = 'Staging refused for '+str(args.root)+': '+str(error)
        print(' '.join(message.splitlines()), file=sys.stderr)
        raise SystemExit(2) from None

if __name__ == '__main__': main()
