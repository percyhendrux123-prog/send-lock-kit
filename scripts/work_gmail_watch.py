#!/usr/bin/env python3
"""Read-only Gmail arrival gate + thread reader. Never sends or modifies mail.

Cron use (the operator creates the job; the agent cannot). The prompt is owner-specific:
  hermes cron create "*/5 * * * *" "<your prompt>" --script work_gmail_watch.py \
      --workdir ~/.hermes/workspaces/work-gmail-watch --deliver telegram --name "Work Gmail alerts"
The script prints one JSON line. {"wakeAgent": false} keeps a quiet tick silent and free; when mail
arrived it wakes the agent with the new messages as context.

Actions: init (once), poll (default), thread <id>, style, ack <ids>, ack-setup, status, arm-test.

Which mailbox: <HERMES_HOME>/axiom-owner.json key "work_gmail". The script checks the signed-in
Gmail address equals it and refuses to read anything otherwise (wrong-account guard).
Token: <HERMES_HOME>/google_token.json is the owner's READ-ONLY Gmail grant, made by the owner at
their Mac with scripts/gmail_auth_readonly.py. This script never creates tokens or runs OAuth.

Owner-specific, NOT in this file: what the agent does with a wake (the cron prompt), the
approval-brief wording, which senders matter, any delivery receipts or outbox.
Not covered by the offline tests; it needs a real Google grant.
"""
import argparse, base64, datetime, fcntl, glob, importlib.util, json, os, re, subprocess, sys, time
from pathlib import Path
HOME = Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes')))
ROOT = HOME/'workspaces/work-gmail-watch'
STATE = ROOT/'state.json'
def _account():
    p = os.environ.get('AXIOM_OWNER_CONFIG') or str(HOME/'axiom-owner.json')
    try:
        a = str(json.loads(Path(p).read_text()).get('work_gmail') or '').strip().lower()
    except (OSError, ValueError):
        a = ''
    if not a:
        raise RuntimeError('work_gmail is not set in axiom-owner.json; no mail read')
    return a

def ensure_google():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        return
    except ImportError:
        pass
    for py in sorted(glob.glob(str(HOME/'installs/*/environments/*/venv/bin/python3')), key=os.path.getmtime, reverse=True):
        if os.path.realpath(py) == os.path.realpath(sys.executable):
            continue
        r = subprocess.run([py,'-c','import google.oauth2.credentials,googleapiclient.discovery'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            os.execv(py,[py,__file__,*sys.argv[1:]])
    raise RuntimeError('Google read-only libraries unavailable; no install or OAuth attempted')

def service():
    ensure_google()
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    c = Credentials.from_authorized_user_file(str(HOME/'google_token.json'))
    if not c.valid and c.refresh_token:
        c.refresh(Request())
    s = build('gmail','v1',credentials=c,cache_discovery=False)
    p = s.users().getProfile(userId='me').execute()
    if p['emailAddress'].lower() != _account():
        raise RuntimeError('Wrong mailbox; no mail read')
    return s,p

def save(data):
    tmp=STATE.with_suffix('.new')
    tmp.write_text(json.dumps(data,indent=2));os.chmod(tmp,0o600);tmp.replace(STATE)

def safe_text(text):
    text=re.sub(r'(?im)\b(password|passcode|verification code|access token|api key|secret)\s*[:=]\s*\S+',r'\1: [REDACTED]',text)
    text=re.sub(r'https?://\S*(?:token=|password=|code=)\S+','[credential-bearing link redacted]',text,flags=re.I)
    return text

def headers(m):
    return {x['name'].lower():x['value'] for x in m.get('payload',{}).get('headers',[])}

def readable(m):
    h=headers(m); plain=[]; html=[]; files=[]
    def walk(p):
        if p.get('filename'): files.append(p['filename'])
        data=p.get('body',{}).get('data')
        if data and p.get('mimeType') in ('text/plain','text/html'):
            t=base64.urlsafe_b64decode(data+'='*(-len(data)%4)).decode('utf-8','replace')
            (plain if p['mimeType']=='text/plain' else html).append(t)
        for child in p.get('parts',[]):walk(child)
    walk(m.get('payload',{}))
    text='\n'.join(plain)
    if not text:
        import html as ht
        text=ht.unescape(re.sub('<[^>]+>',' ', '\n'.join(html)))
    return {'id':m['id'],'threadId':m.get('threadId'),'from':h.get('from'),'to':h.get('to'),'cc':h.get('cc'),'subject':h.get('subject'),'date':h.get('date'),'labels':m.get('labelIds',[]),'body':safe_text(text),'attachments':files}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',nargs='?',default='poll',choices=['init','poll','thread','style','ack','ack-setup','status','arm-test']);ap.add_argument('ids',nargs='*');a=ap.parse_args()
    ROOT.mkdir(parents=True,exist_ok=True);os.chmod(ROOT,0o700)
    with (ROOT/'state.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        st=json.loads(STATE.read_text()) if STATE.exists() else None
        if a.action=='status':
            print(json.dumps(st or {'initialized':False}));return
        if a.action in ('ack','ack-setup'):
            if not st:raise RuntimeError('Watcher not initialized')
            if a.action=='ack-setup':st['setup_pending']=False
            else:
                for mid in a.ids:
                    if mid not in st['pending']:continue
                    if not (ROOT/'drafts'/f'{mid}.md').exists():raise RuntimeError('Save approval brief before acknowledging '+mid)
                    st['pending'].pop(mid);st['reported'].append(mid)
            save(st);print(json.dumps({'ok':True,'pending_count':len(st['pending'])}));return
        s,profile=service()
        if a.action=='arm-test':
            if not st: raise RuntimeError('Initialize before arming')
            st['reported']=list(set(st['reported'])|set(st['pending']))
            st.update(history_id=profile['historyId'], last_scan_epoch=int(time.time()), pending={}, setup_pending=False, test_pending=True)
            save(st);print(json.dumps({'baseline_reset':True,'test_armed':True}));return
        if a.action=='poll' and st and st.get('test_pending'):
            st['test_pending']=False;save(st)
            print('TEST from your agent: Gmail alerts are set up. No action needed.');return
        if a.action=='init':
            if st:raise RuntimeError('Already initialized; refusing reset')
            st={'account':_account(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'last_scan_epoch':int(time.time()),'history_id':profile['historyId'],'pending':{},'reported':[],'setup_pending':True}
            save(st);print(json.dumps({'verified_account':_account(),'initialized':True,'backlog_included':False}));return
        if a.action=='thread':
            if len(a.ids)!=1 or not re.fullmatch('[0-9a-f]+',a.ids[0]):raise RuntimeError('Expected one exact Gmail thread ID')
            t=s.users().threads().get(userId='me',id=a.ids[0],format='full').execute()
            print(json.dumps({'account':_account(),'threadId':t['id'],'messages':[readable(m) for m in t.get('messages',[])]}));return
        if a.action=='style':
            ids=s.users().messages().list(userId='me',q='in:sent',maxResults=20).execute().get('messages',[])
            out=[]
            for row in ids:
                m=readable(s.users().messages().get(userId='me',id=row['id'],format='full').execute())
                m['body']='\n'.join(m['body'].splitlines()[:40]);out.append(m)
            print(json.dumps({'account':_account(),'sent_style_sample':out}));return
        if not st:raise RuntimeError('Initialize before polling')
        candidates=set(); page=None; latest=st['history_id']; now=int(time.time())
        try:
            while True:
                r=s.users().history().list(userId='me',startHistoryId=st['history_id'],historyTypes=['messageAdded'],maxResults=500,pageToken=page).execute()
                latest=r.get('historyId',latest)
                for h in r.get('history',[]):
                    for x in h.get('messagesAdded',[]):candidates.add(x['message']['id'])
                page=r.get('nextPageToken')
                if not page:break
        except Exception as e:
            if getattr(getattr(e,'resp',None),'status',None)!=404:raise
            page=None
            while True:
                r=s.users().messages().list(userId='me',q=f'after:{st["last_scan_epoch"]-300} -in:spam -in:trash -in:drafts',maxResults=500,pageToken=page).execute()
                candidates.update(x['id'] for x in r.get('messages',[]));page=r.get('nextPageToken')
                if not page:break
            latest=profile['historyId']
        for mid in sorted(candidates):
            if mid in st['pending'] or mid in st['reported']:continue
            try:m=s.users().messages().get(userId='me',id=mid,format='metadata',metadataHeaders=['From','To','Subject','Date']).execute()
            except Exception as e:
                if getattr(getattr(e,'resp',None),'status',None)==404:continue
                raise
            labels=set(m.get('labelIds',[]))
            if labels&{'SPAM','TRASH','DRAFT'} or ('SENT' in labels and 'INBOX' not in labels):continue
            h=headers(m)
            if h.get('subject','').lstrip().casefold().startswith('[alert]'):continue
            st['pending'][mid]={'id':mid,'threadId':m['threadId'],'from':h.get('from'),'subject':h.get('subject'),'date':h.get('date')}
        st['history_id']=latest;st['last_scan_epoch']=now;save(st)
        print(json.dumps({'wakeAgent':bool(st['pending']) or st['setup_pending'],'context':{'account':_account(),'setup_pending':st['setup_pending'],'pending':list(st['pending'].values())}}))

if __name__=='__main__':
    from alert_health import transition, plain_reason
    action=sys.argv[1] if len(sys.argv)>1 else 'poll'
    if action!='poll':
        main()
    else:
        import io, contextlib
        try:
            buf=io.StringIO()
            with contextlib.redirect_stdout(buf): main()
            recovery=transition(ROOT/'health.json','Gmail')
            output=buf.getvalue().strip()
            if recovery:
                payload=json.loads(output) if output.startswith('{') else {'wakeAgent':True,'context':{}}
                payload['wakeAgent']=True;payload.setdefault('context',{})['health_notice']=recovery
                print(json.dumps(payload))
            elif output: print(output)
        except Exception as e:
            notice=transition(ROOT/'health.json','Gmail',plain_reason(e))
            print(json.dumps({'wakeAgent':bool(notice),'context':{'health_notice':notice}}))
