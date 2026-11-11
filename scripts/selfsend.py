#!/usr/bin/env python3
"""selfsend.py: Door 1, "send it to yourself".

The agent can email a finished draft to the OWNER's own inboxes so they see exactly
what a real email would look like. It can't reach anyone else: the allowed
addresses live in <HERMES_HOME>/axiom-owner.json (owner_emails, alert_email), the
send-lock blocks the agent from editing that file or this one, and a recipient not on
the list is refused (exit 2). A missing or empty list refuses everything (fail closed).

Usage:
  selfsend.py --to <owner address> --subject "..." --body-file draft.txt [--html] [--dry-run]
  selfsend.py --alert --to <alert address> --subject "[Alert] ..." --body-file msg.txt
  selfsend.py --auth      # the OWNER grants the one Google permission (gmail.send), once
  selfsend.py --status    # shows whether Door 1 is connected (no secrets printed)

Every attempt (sent, refused, dry run) is logged to <HERMES_HOME>/logs/selfsend.log.
Config keys used: owner_emails (list), alert_email (one address, must be in owner_emails
or a separate personal address the owner chose), daily_cap (default 15), alert_cap (default 120).
"""
import argparse
import base64
import glob
import json
import os
import sys
from datetime import datetime, date
from email.mime.text import MIMEText
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
CONFIG = Path(os.environ.get("AXIOM_OWNER_CONFIG") or (HERMES / "axiom-owner.json"))


def _cfg():
    try:
        d = json.loads(CONFIG.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_C = _cfg()
ALLOWED = {str(x).strip().lower() for x in (_C.get("owner_emails") or []) if str(x).strip()}
DAILY_CAP = int(_C.get("daily_cap") or 15)
# Alerts: five-minute check alerts to ONE personal address, counted separately so they
# never eat the normal daily sends.
ALERT_TO = str(_C.get("alert_email") or "").strip().lower()
ALERT_CAP = int(_C.get("alert_cap") or 120)
CLIENT_SECRET = HERMES / "google_client_secret.json"
TOKEN = HERMES / "google_token_send.json"   # separate from the read-only token
LOG = HERMES / "logs" / "selfsend.log"
SCOPE = ["https://www.googleapis.com/auth/gmail.send"]


def log(status, **kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "status": status, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


def sent_today(alert=False):
    if not LOG.exists():
        return 0
    today = date.today().isoformat()
    n = 0
    for line in LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("status") == "sent" and r.get("ts", "").startswith(today) and bool(r.get("alert")) == alert:
            n += 1
    return n


def refuse(kind, what, why, opens):
    msg = (f"REFUSED by selfsend (Door 1). KIND: {kind}.\nWhat was stopped: {what}\n"
           f"Why: {why}\nWhat opens it: {opens}")
    print(msg)
    log("refused", reason=what)
    sys.exit(2)


def google_libs():
    try:
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
        return True
    except ImportError:
        pass
    # Re-run with the Hermes environment that already has the Google libraries.
    for py in sorted(glob.glob(str(HERMES / "installs/*/environments/*/venv/bin/python3")), key=os.path.getmtime, reverse=True):
        if os.path.realpath(py) == os.path.realpath(sys.executable):
            continue
        if os.system(f'"{py}" -c "import googleapiclient, google_auth_oauthlib" 2>/dev/null') == 0:
            os.execv(py, [py, __file__, *sys.argv[1:]])
    return False


def creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    c = Credentials.from_authorized_user_file(str(TOKEN), SCOPE)
    if not c.valid and c.refresh_token:
        c.refresh(Request())
        TOKEN.write_text(c.to_json())
    return c


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--to")
    p.add_argument("--subject")
    p.add_argument("--body-file")
    p.add_argument("--html", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--alert", action="store_true")
    p.add_argument("--auth", action="store_true")
    p.add_argument("--status", action="store_true")
    a = p.parse_args()

    if a.status:
        print(json.dumps({"door": 1, "connected": TOKEN.exists(), "configured": bool(ALLOWED), "allowed_count": len(ALLOWED),
                          "sent_today": sent_today(), "daily_cap": DAILY_CAP,
                          "alerts_today": sent_today(alert=True), "alert_cap": ALERT_CAP, "alert_configured": bool(ALERT_TO)}))
        return

    if a.auth:
        if not google_libs():
            refuse("FREE FIX", "Google libraries missing", "the Google sign-in tools aren't installed in this Python",
                   "run: hermes skills / google-workspace setup.py --install-deps (free), then retry")
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPE)
        c = flow.run_local_server(port=0, prompt="consent")
        TOKEN.write_text(c.to_json())
        os.chmod(TOKEN, 0o600)
        log("auth", scope=SCOPE[0])
        print("Door 1 connected: this agent can now send emails to " + " and ".join(sorted(ALLOWED)) + " only.")
        return

    to = (a.to or "").strip().lower()
    if a.alert and ALERT_TO and to == ALERT_TO:
        pass  # the alert address is allowed for --alert even if it is not in owner_emails
    elif to not in ALLOWED:
        refuse("PERMISSION", f"an email to {to or '(no address)'}",
               "Door 1 only reaches the owner's own inboxes (list is in axiom-owner.json); anyone else needs Door 2",
               "Door 2 (send to others after the owner approves each email), the owner's OK and Axiom's OK")
    if not a.subject or not a.body_file or not Path(a.body_file).expanduser().exists():
        refuse("CAPABILITY", "an email with no subject or body file", "selfsend needs --subject and --body-file",
               "add both and run again")
    if a.alert:
        if not ALERT_TO or to != ALERT_TO:
            refuse("PERMISSION", f"an alert to {to}", "alerts go only to the one alert address in axiom-owner.json",
                   "send the alert to the configured alert address (see --status)")
        if sent_today(alert=True) >= ALERT_CAP:
            refuse("PERMISSION", f"alert #{ALERT_CAP + 1} today", f"alerts are capped at {ALERT_CAP} a day",
                   "tomorrow, or Axiom raises the cap with the owner's OK")
    elif sent_today() >= DAILY_CAP:
        refuse("PERMISSION", f"email #{DAILY_CAP + 1} today", f"Door 1 is capped at {DAILY_CAP} test emails a day",
               "tomorrow, or Axiom raises the cap with the owner's OK")
    body = Path(a.body_file).expanduser().read_text()
    if a.dry_run:
        log("dry_run", to=to, subject=a.subject, chars=len(body))
        print(f"DRY RUN OK: would send '{a.subject}' to {to} ({len(body)} chars). Nothing sent.")
        return
    if not TOKEN.exists():
        refuse("PERMISSION", "a send before Door 1 is connected",
               "the owner hasn't granted the one Google permission this needs (send as them, to themselves only)",
               "the owner runs: selfsend.py --auth and approves the Google screen. No cost.")
    if not google_libs():
        refuse("FREE FIX", "Google libraries missing", "the Google tools aren't installed in this Python",
               "google-workspace setup.py --install-deps (free)")
    from googleapiclient.discovery import build
    msg = MIMEText(body, "html" if a.html else "plain")
    msg["to"] = to
    msg["subject"] = a.subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc = build("gmail", "v1", credentials=creds(), cache_discovery=False)
    res = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    log("sent", to=to, subject=a.subject, id=res.get("id"), alert=bool(a.alert))
    print(f"SENT to {to}: '{a.subject}' (Gmail id {res.get('id')}). Check that inbox.")


if __name__ == "__main__":
    main()
