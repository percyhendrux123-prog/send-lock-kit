"""Container-only fake integration assertions. Invoked by host Docker script."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import urllib.error
import urllib.request
from sender import process, http_delivery


def request(url, data=None):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(url,data=data,timeout=2) as r:
        assert r.status == 200
        return r.read()


def denied(label, action):
    try:
        action()
    except urllib.error.HTTPError as error:
        error.close()
        raise AssertionError('HTTP response proves network was reached: '+label) from error
    except (OSError, urllib.error.URLError):
        print('PASS '+label)
        return
    raise AssertionError('BOUNDARY OPEN: '+label)


def socket25(host):
    with socket.create_connection((host,25),timeout=2) as s:
        assert s.recv(100).startswith(b'220 FAKE SMTP')


def http_network_denied(label, url, method='GET', body=None):
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname,u.port,timeout=2)
    try:
        try: conn.connect()
        except OSError:
            print('PASS '+label)
            return
        # Once connect succeeds the boundary is open, even if a later response
        # times out. Attempt the fake request but never count that as a denial.
        try: conn.request(method,u.path or '/',body=body)
        except OSError: pass
        raise AssertionError('NETWORK REACHABLE: '+label)
    finally: conn.close()


def connect25(host):
    with socket.create_connection((host,25),timeout=2): pass


def proxy_denied(method, address, body=None, host='egress-gateway', port=8080):
    import http.client
    conn = http.client.HTTPConnection(host,port,timeout=2)
    try:
        conn.request(method,address,body=body)
        response = conn.getresponse()
        assert response.status == 403, (method,address,response.status)
        response.read()
    finally: conn.close()
    print('PASS gateway rejects actual '+method+' proxy request with HTTP 403')


def boundary(ip):
    http_network_denied('outside POST fails at network','http://'+ip+':8080/post','POST',b'fake')
    http_network_denied('outside GET fails at network','http://'+ip+':8080/get')
    denied('raw socket port 25 fails at network', lambda: connect25(ip))
    denied('worker cannot chmod owner-owned outbox', lambda: Path('/outbox').chmod(0))
    denied('approvals path is not writable', lambda: Path('/approvals/forged.json').write_text('{}'))
    denied('plugin file is not writable', lambda: Path('/guard/plugins/send-lock/__init__.py').write_text('# bypass'))
    denied('owner config is not writable', lambda: Path('/guard/axiom-owner.json').write_text('{"step":4}'))
    denied('Hermes config is not writable', lambda: Path('/guard/config.yaml').write_text('plugins: {}'))
    denied('sender credential is absent', lambda: Path('/data/secrets/send-token').read_text())
    denied('reserved outside DNS lookup fails (not packet-capture proof)', lambda: socket.getaddrinfo('no-egress.invalid',443))
    import os
    for method in ['GET','POST']:
        proxy_denied(method,'http://'+ip+':8080/get',b'fake' if method=='POST' else None)
    proxy_denied('CONNECT',ip+':443')
    proxy_denied('POST','/google/read/mail',b'fake')
    assert json.loads(request(os.environ['GOOGLE_READ_BASE_URL']+'/mail')) == {'fake':True}
    assert json.loads(request(os.environ['MODEL_BASE_URL']+'/chat',b'{}')) == {'fake':True}
    assert json.loads(request(os.environ['OWNER_INBOX_BASE_URL']+'/list')) == {'fake':True}
    print('PASS configured fake Google GET, owner GET and model POST reachable')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['control','boundary','queue','hash','send','receipts','audit','url-control','url-denied'])
    p.add_argument('value',nargs='?')
    p.add_argument('--body',default='hello from fake demo')
    args = p.parse_args()
    if args.action == 'control':
        for data in [None,b'fake']:
            assert request('http://'+args.value+':8080/control',data)
        socket25(args.value)
        print('PASS outside control: GET, POST and SMTP are live')
    elif args.action == 'url-control':
        assert request(args.value)
        print('PASS fake host listener is live from outside network')
    elif args.action == 'url-denied':
        http_network_denied('host bridge listener unreachable from worker',args.value)
    elif args.action == 'boundary': boundary(args.value)
    elif args.action == 'queue':
        p = Path('/outbox')/args.value
        p.write_text(json.dumps({'kind':'note','to':'fake-owner','body':args.body}))
        p.chmod(0o640)
    elif args.action == 'hash':
        print(hashlib.sha256((Path('/data/outbox')/args.value).read_bytes()).hexdigest())
    elif args.action == 'send':
        print(process(Path('/data'), args.value, http_delivery('http://fake-receiver:8080/send')))
    elif args.action == 'receipts':
        p = Path('/receipts/received.jsonl')
        rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        assert len(rows) == int(args.value), rows
        if rows:
            assert json.loads(rows[0]['raw']) == {'kind':'note','to':'fake-owner','body':'hello from fake demo'}
        print('PASS receiver count and exact fake payload: '+str(len(rows)))
    elif args.action == 'audit':
        rows = [json.loads(x) for x in Path('/data/state/audit.jsonl').read_text().splitlines()]
        events = [r['event'] for r in rows]
        assert events == json.loads(args.value), events
        print('PASS audit events: '+','.join(events))

if __name__ == '__main__': main()
