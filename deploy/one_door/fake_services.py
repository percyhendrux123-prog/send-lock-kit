"""Fake local receiver/API and live SMTP port used as negative-test controls."""
import argparse
import http.server
import json
from pathlib import Path
import socketserver
import threading


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--receipts',default='/receipts/received.jsonl')
    p.add_argument('--smtp',action='store_true')
    p.add_argument('--http-port',type=int,default=8080)
    args = p.parse_args()
    class HTTP(http.server.BaseHTTPRequestHandler):
        def answer(self):
            size = int(self.headers.get('Content-Length','0'))
            if size > 65536:
                self.send_error(413); return
            raw = self.rfile.read(size)
            if self.path == '/send':
                if self.command != 'POST' or self.headers.get('Authorization') != 'Bearer FAKE-DEMO-ONLY':
                    self.send_error(403); return
                # Receipts contain only the fake data made in the host test.
                with Path(args.receipts).open('a') as f:
                    f.write(json.dumps({'raw':raw.decode(),'path':self.path})+'\n')
                content = b'accepted'
            else:
                content = b'{"fake":true}'
            self.send_response(200)
            self.send_header('Content-Length',str(len(content)))
            self.end_headers(); self.wfile.write(content)
        do_GET = do_POST = answer
        def log_message(self,*args): pass
    if args.smtp:
        class SMTP(socketserver.BaseRequestHandler):
            def handle(self): self.request.sendall(b'220 FAKE SMTP control\r\n')
        smtp = socketserver.ThreadingTCPServer(('0.0.0.0',25), SMTP)
        threading.Thread(target=smtp.serve_forever,daemon=True).start()
    server = http.server.ThreadingHTTPServer(('0.0.0.0',args.http_port), HTTP)
    print('FAKE_PORT='+str(server.server_port),flush=True)
    server.serve_forever()

if __name__ == '__main__': main()
