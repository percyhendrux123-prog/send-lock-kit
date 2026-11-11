#!/usr/bin/env python3
"""for_axiom.py: the operator relay. The one way the owner's agent leaves a note for the operator.

This script sends nothing. It writes one small request file into

    <HERMES_HOME>/workspaces/FOR-AXIOM/<timestamp>-<kind>.md

An operator-side watcher (scripts/stall_watch.py) can list that folder over SSH every few
minutes and forward each new file to the operator's own alert channel. The folder name and the
script name are kept because the code and tests use them. A request waits in the folder (it is
not lost) while the machine is offline. A kanban card on the agent's machine does not reach the
operator, which is why this script exists. The legacy send-lock matrix allows a plain run of this
script and blocks edits to it; the registered kit policy does not allow it (see docs/ONE-DOOR.md).

Usage (by the agent, on the owner's request or when a block or a failure needs the operator):
  for_axiom.py --kind door-request   --what "one line: what is needed and why"
  for_axiom.py --kind update-request --what "owner asked for the newest Hermes; check said 0.23.1 is out"
  for_axiom.py --kind problem        --what "Gmail alerts said 'access refused' twice today"
  for_axiom.py --kind problem        --what-file /tmp/req.txt   # text from a file (see below)
  for_axiom.py --status              # requests written in the last 7 days (names only)

Use --what-file when the text names a command (for example the Hermes update command): Hermes' own
command guard flags those words anywhere on a command line, even inside quotes, and stops the run.
Write the text to a file with the file tool first, then pass the file. Same scrub and limits apply.

--what-file must be a plain text file in /tmp, the machine's temp folder ($TMPDIR) or
<HERMES_HOME>/workspaces/ (after following links), must not be a secret file by name (.env,
google_token*, client_secret*, credentials.json, axiom-owner.json, keys, anything under plugins/
or .ssh/) and must be under 20 KB. Anything else is refused.

Privacy: never put a client's or customer's full name, health detail, account detail, phone
number or email address in --what. Phone-number-like digit runs and email addresses are
replaced with [removed] before the file is written. Secrets are never relayed: token-shaped
strings (Google refresh/access tokens and client secrets, Telegram bot tokens, API keys, JWTs,
private keys, NAME=value / "name": "value" pairs for token, secret, password or key fields, and
any long random-looking string) are replaced with [secret removed]. The watcher scrubs the
same way again before it prints. Max 600 characters, 10 requests a day.
Exit 0 = written. Exit 2 = refused (message says why). Prints no secrets.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
OUTBOX = HERMES / "workspaces" / "FOR-AXIOM"
KINDS = ("door-request", "update-request", "problem", "other")
MAX_CHARS = 600
DAILY_CAP = 10

EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_RX = re.compile(r"\+?\d[\d\s().-]{6,}\d")
DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_FILE_BYTES = 20000

# Secrets. Same list in stall_watch.py (the watcher scrubs again before printing).
SECRET = "[secret removed]"
SECRET_RXES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), SECRET),
    (re.compile(r"(?i)([\"']?[\w.-]*(token|secret|password|passwd|api[_-]?key|apikey|private[_-]?key|"
                r"credential)[\w.-]*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|(?=[^\s,}]*\d)[^\s,}]{8,})"),
     r"\1" + SECRET),
    (re.compile(r"1//[\w-]{10,}"), SECRET),                       # Google refresh token
    (re.compile(r"ya29\.[\w.-]{10,}"), SECRET),                   # Google access token
    (re.compile(r"GOCSPX-[\w-]{4,}"), SECRET),                    # Google OAuth client secret
    (re.compile(r"\b\d{6,12}:[\w-]{30,}"), SECRET),               # Telegram bot token
    (re.compile(r"AIza[\w-]{30,}"), SECRET),                      # Google API key
    (re.compile(r"\b(sk|pk|rk)-[\w-]{16,}"), SECRET),             # OpenAI / Anthropic / Stripe style keys
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[\w]{10,}"), SECRET),  # Stripe
    (re.compile(r"\bxox[abprs]-[\w-]{10,}"), SECRET),             # Slack
    (re.compile(r"\bgh[pousr]_\w{20,}"), SECRET),                 # GitHub
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}"), SECRET),  # JWT
]
LONG_RX = re.compile(r"[A-Za-z0-9_+=/-]{32,}")
SECRET_NAME_RX = re.compile(
    r"(^\.env|google_token|client_secret|credentials\.json|axiom-owner|auth\.json|^id_(rsa|ed25519|ecdsa)|"
    r"\.netrc$|\.(pem|p12|key)$|token\.json$|\.db$|\.sqlite\d?$)", re.I)
SECRET_DIRS = {"plugins", ".ssh", "keychains", ".gnupg", "secrets"}


def _long(m):
    s = m.group(0)
    if re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s):
        return SECRET  # random-looking: upper + lower + digits in one long run
    return s


def redact(text):
    for rx, rep in SECRET_RXES:
        text = rx.sub(rep, text)
    return LONG_RX.sub(_long, text)


def safe_what_file(name):
    """Return the text of --what-file, or refuse. Only small text files in /tmp, $TMPDIR or
    <HERMES_HOME>/workspaces/ (links followed), never a secret file by name."""
    given = Path(name).expanduser()
    real = Path(os.path.realpath(given))
    roots = [Path(os.path.realpath(r)) for r in
             ("/tmp", "/private/tmp", os.environ.get("TMPDIR") or "/tmp", str(HERMES / "workspaces"))]
    if not any(real == r or r in real.parents for r in roots):
        refuse("--what-file must be in /tmp, the temp folder or %s/workspaces/. Write the text there "
               "with the file tool first." % HERMES)
    for p in (given, real):
        if SECRET_NAME_RX.search(p.name) or any(part.lower() in SECRET_DIRS for part in p.parts):
            refuse("--what-file names a secret file. Never send a token, key or config file, not even to "
                   "Axiom. Describe the problem in plain words.")
    try:
        if not real.is_file() or real.stat().st_size > MAX_FILE_BYTES:
            refuse("--what-file must be a plain text file under %d bytes." % MAX_FILE_BYTES)
        return real.read_text(errors="replace")[:4000]
    except OSError:
        refuse("--what-file could not be read.")


def _phone(m):
    s = m.group(0)
    if DATE_RX.match(s) and sum(c.isdigit() for c in s[10:]) < 7:
        return s  # a date, maybe with a time, not a phone number
    return "[removed]" if sum(c.isdigit() for c in s) >= 10 else s


def scrub(text):
    text = redact(text)
    text = EMAIL_RX.sub("[removed]", text)
    text = PHONE_RX.sub(_phone, text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:MAX_CHARS]


def refuse(why):
    print("REFUSED by for_axiom.py: " + why + " Nothing was written.")
    sys.exit(2)


def today_count():
    if not OUTBOX.exists():
        return 0
    stamp = date.today().strftime("%Y%m%d")
    return sum(1 for p in OUTBOX.glob(stamp + "-*.md"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=KINDS)
    ap.add_argument("--what")
    ap.add_argument("--what-file")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        cutoff = datetime.now().timestamp() - 7 * 86400
        files = sorted(p.name for p in OUTBOX.glob("*.md") if p.stat().st_mtime >= cutoff) if OUTBOX.exists() else []
        print(json.dumps({"outbox": str(OUTBOX), "last_7_days": files, "today": today_count(), "daily_cap": DAILY_CAP}))
        return
    if a.what_file and not a.what:
        a.what = safe_what_file(a.what_file)
    if not a.kind or not a.what:
        refuse("--kind and --what (or --what-file) are both required.")
    what = scrub(a.what)
    if len(what) < 5:
        refuse("--what is empty after removing contact details. Say what is needed in plain words.")
    if today_count() >= DAILY_CAP:
        refuse("%d requests were already written today. Tell the owner to text Axiom directly." % DAILY_CAP)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    base = now.strftime("%Y%m%d-%H%M%S") + "-" + a.kind
    path = OUTBOX / (base + ".md")
    n = 1
    while path.exists():
        n += 1
        path = OUTBOX / ("%s-%d.md" % (base, n))
    path.write_text("kind: %s\nwritten: %s\nwhat: %s\n" % (a.kind, now.isoformat(timespec="seconds"), what))
    os.chmod(path, 0o600)
    print("WRITTEN for Axiom: %s. Axiom's watcher picks it up within about 5 minutes while this Mac is online. "
          "Tell the owner it is with Axiom; for anything urgent they can also text Axiom." % path.name)


if __name__ == "__main__":
    main()
