#!/usr/bin/env python3
"""Offline tests for scripts/stall_watch.py and scripts/for_axiom.py. Plain python, no pytest:

    python3 tests/test_stall_watch.py

No host is contacted and nothing outside a temp folder is touched. A fake `ssh` is put first
on PATH; HOME points at the temp folder, so the watcher's state file lands there too.
Part A feeds canned remote output (every health case, the fail-closed rules, the relay).
Part B runs the REAL remote shell script under bash against a fake client home that has no bot
token (so it never calls Telegram) and a request written by the real for_axiom.py: end to end
from the agent's request file to the relayed line.
"""
import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent
WATCH = PACK / "scripts" / "stall_watch.py"
FOR_AXIOM = PACK / "scripts" / "for_axiom.py"

tmp = Path(tempfile.mkdtemp(prefix="stallwatch-test-"))
fakebin = tmp / "bin"
fakebin.mkdir()
ssh = fakebin / "ssh"
ssh.write_text("#!/bin/bash\n"
               "if [ -n \"$FAKE_RUN_REMOTE\" ]; then for a; do last=$a; done; cd \"$FAKE_CLIENT_HOME\" || exit 255; "
               "HOME=\"$FAKE_CLIENT_HOME\" exec bash -c \"$last\"; fi\n"
               "[ -f \"$FAKE_OUT\" ] && cat \"$FAKE_OUT\"\nexit ${FAKE_RC:-0}\n")
ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
canned = tmp / "remote.out"

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print("FAIL  %-60s %s" % (label, detail))


def run(name, out=None, rc=0, extra_env=None):
    env = {"HOME": str(tmp), "PATH": "%s:%s" % (fakebin, os.environ.get("PATH", "")),
           "FAKE_OUT": str(canned), "FAKE_RC": str(rc)}
    env.update(extra_env or {})
    if out is None:
        canned.unlink(missing_ok=True)
    else:
        canned.write_text(out)
    r = subprocess.run([sys.executable, str(WATCH), "--name", name, "--host", "test@fake.invalid"],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.stdout.strip().splitlines(), r.returncode


def status(p, g="up", last="2026-10-27 09:00"):
    return "STATUS|%s|%s|%s\n" % (p, g, last)


def req(name, text):
    b = lambda s: base64.b64encode(s.encode()).decode()
    return "REQ|%s|%s\n" % (b(name), b(text))


# --- Part A: canned remote output ---------------------------------------------------------
# healthy: silent
o, rc = run("a1", status(0)); check("healthy tick is silent", o == [] and rc == 0, o)
o, _ = run("a1", status(0)); check("healthy again is silent", o == [], o)
# pending stall: alert on the 2nd strike, once; recovery line once
o, _ = run("a2", status(3)); check("stall strike 1 silent", o == [], o)
o, _ = run("a2", status(3)); check("stall strike 2 alerts", len(o) == 1 and "STALL" in o[0] and "3 Telegram" in o[0], o)
o, _ = run("a2", status(3)); check("stall strike 3 does not repeat", o == [], o)
o, _ = run("a2", status(0)); check("recovery printed once", len(o) == 1 and "recovered" in o[0], o)
o, _ = run("a2", status(0)); check("after recovery silent", o == [], o)
# gateway down
run("a3", status(0, "down")); o, _ = run("a3", status(0, "down"))
check("gateway down alerts on strike 2", len(o) == 1 and "gateway is not running" in o[0], o)
# unreachable (no output, ssh exit 255)
run("a4", None, 255); o, _ = run("a4", None, 255)
check("unreachable alerts on strike 2", len(o) == 1 and "unreachable" in o[0], o)
# wrong hermes home (exit 3)
run("a5", None, 3); o, _ = run("a5", None, 3)
check("missing Hermes folder alerts", len(o) == 1 and "not found" in o[0], o)
# F6: token missing (-2) and token rejected (-3) FAIL CLOSED
run("a6", status(-2)); o, _ = run("a6", status(-2))
check("missing token alerts (fail closed)", len(o) == 1 and "no Telegram bot token" in o[0], o)
run("a7", status(-3)); o, _ = run("a7", status(-3))
check("revoked token alerts (fail closed)", len(o) == 1 and "rejected their bot token" in o[0], o)
# F6: unknown (-1) never counts as healthy
o1, _ = run("a8", status(-1)); o2, _ = run("a8", status(-1)); o3, _ = run("a8", status(-1))
check("unknown x2 silent", o1 == [] and o2 == [], (o1, o2))
check("unknown x3 alerts UNKNOWN", len(o3) == 1 and "UNKNOWN" in o3[0], o3)
o, _ = run("a8", status(-1)); check("unknown x4 does not repeat", o == [], o)
o, _ = run("a8", status(0)); check("real recovery after unknown alert", len(o) == 1 and "recovered" in o[0], o)
# F6: an unknown tick while a stall is alerted gives NO false "recovered" and keeps the alert
run("a9", status(2)); run("a9", status(2))
o, _ = run("a9", status(-1)); check("unknown during alerted stall: no false recovery", o == [], o)
st = json.loads((tmp / ".hermes" / "cache" / "a9_tg_stall.json").read_text())
check("unknown during alerted stall keeps alerted + strikes", st["alerted"] is True and st["strikes"] == 2, st)
o, _ = run("a9", status(0)); check("then a real recovery", len(o) == 1 and "recovered" in o[0], o)
# unknown ticks do not reset a stall's strike count
run("a10", status(4)); run("a10", status(-1)); o, _ = run("a10", status(4))
check("strikes survive an unknown tick", len(o) == 1 and "STALL" in o[0], o)
# garbage pending value -> unknown, not healthy
o1, _ = run("a11", "STATUS|abc|up|\n"); run("a11", "STATUS|abc|up|\n"); o3, _ = run("a11", "STATUS|abc|up|\n")
check("unparseable pending is unknown, alerts on 3rd", o1 == [] and len(o3) == 1 and "UNKNOWN" in o3[0], (o1, o3))
# relay: each new request once, while healthy and while unhealthy
r1 = req("20261027-090000-door-request.md", "kind: door-request\nwhat: owner asks for a CRM door")
o, _ = run("b1", status(0) + r1)
check("new request relayed once", len(o) == 1 and "FOR AXIOM (20261027-090000-door-request.md)" in o[0]
      and "CRM door" in o[0], o)
o, _ = run("b1", status(0) + r1); check("same request not relayed again", o == [], o)
r2 = req("20261027-091000-problem.md", "what: alerts look stuck")
o, _ = run("b1", status(0) + r1 + r2); check("second request relayed, first not", len(o) == 1 and "091000" in o[0], o)
run("b2", status(5) + r1); o, _ = run("b2", status(5) + r1)
check("relay works while a stall is alerted", len(o) == 1 and "STALL" in o[0], o)
o, _ = run("b3", status(0) + req("x.md", "a" * 5000))
check("relayed text is capped", len(o) == 1 and len(o[0]) < 800, len(o[0]) if o else o)
o, _ = run("b4", status(0) + "REQ|!!notbase64|zz\n"); check("malformed REQ line ignored", o == [], o)

# --- Part B: real remote script + real for_axiom.py, no network ------------------------------
client = tmp / "client-home" / ".hermes"
(client / "logs").mkdir(parents=True)
(client / ".env").write_text("TELEGRAM_ALLOWED_USERS=1,2\n")   # no token: the script must not call Telegram
fa_env = {"HERMES_HOME": str(client), "PATH": os.environ.get("PATH", ""), "HOME": str(tmp / "client-home")}
w = subprocess.run([sys.executable, str(FOR_AXIOM), "--kind", "update-request", "--what",
                    "Owner asked to run hermes update; reach them at 555-555-0100 or sam@example.test"],
                   capture_output=True, text=True, env=fa_env)
check("for_axiom.py writes a request", w.returncode == 0 and "WRITTEN for Axiom" in w.stdout, w.stdout + w.stderr)
files = list((client / "workspaces" / "FOR-AXIOM").glob("*.md"))
body = files[0].read_text() if files else ""
check("request file scrubs phone and email", len(files) == 1 and "[removed]" in body and "555" not in body
      and "example.test" not in body, body)
check("request file is mode 600", files and oct(files[0].stat().st_mode & 0o777) == "0o600")
bad = subprocess.run([sys.executable, str(FOR_AXIOM), "--kind", "problem"], capture_output=True, text=True, env=fa_env)
check("for_axiom.py refuses without --what", bad.returncode == 2 and "REFUSED" in bad.stdout, bad.stdout)
dates = subprocess.run([sys.executable, str(FOR_AXIOM), "--kind", "other", "--what", "since 2026-10-27 the check fails"],
                       capture_output=True, text=True, env=fa_env)
newest = sorted((client / "workspaces" / "FOR-AXIOM").glob("*-other.md"))
check("a date is not scrubbed as a phone number", dates.returncode == 0 and newest
      and "2026-10-27" in newest[-1].read_text(), newest[-1].read_text() if newest else dates.stdout)
wf = client / "workspaces" / "req.txt"   # r2: --what-file must be in /tmp, $TMPDIR or <HH>/workspaces/
wf.write_text("Owner asked to run hermes " + "update today.\nCall back at 555 555 0100.")
w2 = subprocess.run([sys.executable, str(FOR_AXIOM), "--kind", "update-request", "--what-file", str(wf)],
                    capture_output=True, text=True, env=fa_env)
upd = sorted((client / "workspaces" / "FOR-AXIOM").glob("*-update-request*.md"))
txt = " | ".join(f.read_text() for f in upd)
check("for_axiom.py --what-file writes the text, scrubbed, one line", w2.returncode == 0 and len(upd) == 2
      and "update today. Call back at [removed]" in txt, (w2.stdout, txt))
st = subprocess.run([sys.executable, str(FOR_AXIOM), "--status"], capture_output=True, text=True, env=fa_env)
check("for_axiom.py --status lists names only", st.returncode == 0 and json.loads(st.stdout)["today"] == 3, st.stdout)
e2e = {"FAKE_RUN_REMOTE": "1", "FAKE_CLIENT_HOME": str(tmp / "client-home")}
o, _ = run("e2e", "", 0, e2e)
relayed = [x for x in o if "FOR AXIOM" in x]
check("real remote script relays all three requests", len(relayed) == 3 and any("update-request" in x for x in relayed), o)
o, _ = run("e2e", "", 0, e2e)
check("missing token (real script) alerts on strike 2, no re-relay",
      len(o) == 1 and ("no Telegram bot token" in o[0] or "gateway is not running" in o[0]), o)

# --- Part C: secrets never travel ------------------------------------
# Fake secrets only (never real ones). Built in pieces so no file in the repo holds a token-shaped string.
F_REFRESH = "1//" + "0gFAKErefreshTOKEN" + "x9" * 12
F_CLIENT = "GOCSPX-" + "FAKEclientSECRET42"
F_ACCESS = "ya29." + "a0FAKEaccessTOKEN" + "z1" * 10
F_BOT = "123456789:" + "AAFakeBotTokenFakeBot" + "Q7" * 8
F_SK = "sk-" + "ant-FAKEapiKEY" + "k3" * 10
F_LONG = "Zq8" * 14
fakes = [F_REFRESH, F_CLIENT, F_ACCESS, F_BOT, F_SK, F_LONG]
outbox = client / "workspaces" / "FOR-AXIOM"


def n_out():
    return len(list(outbox.glob("*.md")))


def fa(*args, env=None):
    return subprocess.run([sys.executable, str(FOR_AXIOM)] + list(args), capture_output=True, text=True,
                          env=env or fa_env)


tok = {"token": F_ACCESS, "refresh_token": F_REFRESH, "client_id": "x.apps.googleusercontent.com",
       "client_secret": F_CLIENT}
(client / "google_token_send.json").write_text(json.dumps(tok))
(client / "google_token.json").write_text(json.dumps(tok))
(client / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % F_BOT)
before = n_out()
for label, path in [("send token", client / "google_token_send.json"), ("read token", client / "google_token.json"),
                    (".env", client / ".env"), ("owner config", client / "axiom-owner.json")]:
    r = fa("--kind", "problem", "--what-file", str(path))
    check("for_axiom.py refuses --what-file %s (outside the safe area)" % label,
          r.returncode == 2 and "REFUSED" in r.stdout and not any(f in r.stdout for f in fakes), r.stdout)
# A secret file by name is refused even inside the safe area, and a link out of the safe area is followed.
(client / "workspaces" / "google_token.json").write_text(json.dumps(tok))
(client / "workspaces" / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % F_BOT)
os.symlink(client / "google_token_send.json", client / "workspaces" / "notes.txt")
for label, path in [("token by name in workspaces", client / "workspaces" / "google_token.json"),
                    (".env by name in workspaces", client / "workspaces" / ".env"),
                    ("link to the send token", client / "workspaces" / "notes.txt")]:
    r = fa("--kind", "problem", "--what-file", str(path))
    check("for_axiom.py refuses %s" % label, r.returncode == 2 and "REFUSED" in r.stdout, r.stdout)
big = client / "workspaces" / "big.txt"
big.write_text("x" * 30000)
r = fa("--kind", "problem", "--what-file", str(big))
check("for_axiom.py refuses a --what-file over 20 KB", r.returncode == 2, r.stdout)
check("no request written by any refused --what-file", n_out() == before, n_out() - before)
# Token-shaped strings are stripped from --what and from an allowed --what-file.
r = fa("--kind", "problem", "--what", "Gmail broke. refresh %s secret %s access %s bot %s key %s blob %s end"
       % (F_REFRESH, F_CLIENT, F_ACCESS, F_BOT, F_SK, F_LONG))
newest = max(outbox.glob("*.md"), key=lambda p: p.stat().st_mtime_ns)
body = newest.read_text()
check("for_axiom.py strips every token shape from --what", r.returncode == 0 and "Gmail broke." in body
      and "end" in body and body.count("[secret removed]") == 6 and not any(f in body for f in fakes), body)
jf = client / "workspaces" / "pasted.txt"
jf.write_text("The agent pasted this: " + json.dumps(tok) + "\nTELEGRAM_BOT_TOKEN=" + F_BOT + "\nAPI_KEY='abc12345xyz'")
r = fa("--kind", "problem", "--what-file", str(jf))
newest = max(outbox.glob("*.md"), key=lambda p: p.stat().st_mtime_ns)
body = newest.read_text()
check("for_axiom.py strips JSON and NAME=value secrets from --what-file", r.returncode == 0
      and "The agent pasted this" in body and not any(f in body for f in fakes) and "abc12345xyz" not in body, body)
r = fa("--kind", "problem", "--what", "the token: expired at 2026-10-27 09:00, plugins look fine")
newest = max(outbox.glob("*.md"), key=lambda p: p.stat().st_mtime_ns)
check("plain words about a token are kept", "the token: expired at 2026-10-27 09:00" in newest.read_text(),
      newest.read_text())
# Axiom side scrubs again, whoever wrote the file (defence in depth for the FOR-AXIOM folder).
raw = "what: written by hand " + " ".join(fakes) + " mail sam@example.test or 555-555-0100"
o, _ = run("c1", status(0) + req("20261027-100000-problem.md", raw))
check("stall_watch scrubs secrets, email and phone before printing", len(o) == 1 and "written by hand" in o[0]
      and not any(f in o[0] for f in fakes) and "example.test" not in o[0] and "555" not in o[0], o)
o, _ = run("c2", status(0) + req("bad|name\x1b[31m.md", "what: hi there"))
check("stall_watch hides an odd file name", len(o) == 1 and "[odd file name]" in o[0] and "\x1b" not in o[0], o)
many = "".join(req("20261027-11%04d-other.md" % i, "what: request %d" % i) for i in range(12))
o1, _ = run("c3", status(0) + many)
o2, _ = run("c3", status(0) + many)
o3, _ = run("c3", status(0) + many)
check("stall_watch relays at most 10 per tick, the rest next tick, then nothing",
      len(o1) == 10 and len(o2) == 2 and o3 == [], (len(o1), len(o2), o3))

print("stall_watch + for_axiom: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
