"""Two-strike outage transition, no delivery operations."""
import json, os
from pathlib import Path

def transition(path, label, reason=None):
    path=Path(path)
    s=json.loads(path.read_text()) if path.exists() else {'failures':0,'alerted':False}
    line=None
    if reason is not None:
        s['failures']+=1
        if s['failures']>=2 and not s['alerted']:
            s['alerted']=True
            line=f'{label} check failing: {reason}'
    else:
        if s['alerted']: line=f'{label} check back working'
        s={'failures':0,'alerted':False}
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.new');tmp.write_text(json.dumps(s));os.chmod(tmp,0o600);tmp.replace(path)
    return line

def plain_reason(e):
    status=getattr(getattr(e,'resp',None),'status',None)
    if status in (401,403):return 'read-only access was refused; authentication needs checking'
    if isinstance(e,FileNotFoundError):return 'required local file is missing'
    if isinstance(e,TimeoutError):return 'the service did not respond in time'
    return 'read-only check could not complete ('+type(e).__name__+')'
