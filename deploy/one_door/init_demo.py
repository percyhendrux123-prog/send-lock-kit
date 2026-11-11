"""Initialize demo-only named volumes. Root one-shot; never starts Hermes."""
import os
from pathlib import Path
import shutil


def directory(path, uid, gid, mode):
    p = Path(path); p.mkdir(parents=True,exist_ok=True)
    os.chown(p,uid,gid); p.chmod(mode)


def main():
    directory('/data/outbox',10003,10002,0o2770)  # owner owns directory; worker can create drafts, not chmod it
    directory('/data/approvals',10003,10003,0o755)
    directory('/data/guard',10003,10003,0o755)
    directory('/data/state',10002,10002,0o700)
    directory('/data/secrets',10002,10002,0o700)
    directory('/receipts',10004,10004,0o700)
    for name in ['config.yaml','axiom-owner.json','egress.json']:
        p = Path('/data/guard')/name
        if not p.exists(): shutil.copyfile(Path('/kit')/name,p)
        os.chown(p,10003,10003); p.chmod(0o644)
    directory('/data/guard/plugins',10003,10003,0o755)
    directory('/data/guard/plugins/send-lock',10003,10003,0o755)
    for name in ['__init__.py','plugin.yaml']:
        p = Path('/data/guard/plugins/send-lock')/name
        if not p.exists(): shutil.copyfile(Path('/plugin')/name,p)
        os.chown(p,10003,10003); p.chmod(0o444)
    p = Path('/data/secrets/send-token')
    if not p.exists(): p.write_text('FAKE-DEMO-ONLY')
    os.chown(p,10002,10002); p.chmod(0o400)

if __name__ == '__main__': main()
