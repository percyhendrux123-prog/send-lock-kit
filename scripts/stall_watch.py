#!/usr/bin/env python3
"""Telegram stall watcher and operator-relay reader (operator side, read-only). Template: copy per owner machine.

Meant to run on the operator's own machine every 5 minutes as a no-agent cron job. One SSH hop to
the OWNER'S MAC does two read-only things:

1. Stall check. It asks Telegram, from the owner's Mac, how many updates are waiting for the
   owner's bot (getWebhookInfo pending_update_count). A healthy gateway drains that to 0 within
   seconds. Messages left waiting = the silent stall (bot "connected", inbound not consumed). It also
   checks that the owner's gateway process is running.
2. Relay reader. It lists the request files the owner's agent wrote with for_axiom.py into
   <their HERMES_HOME>/workspaces/FOR-AXIOM/ and prints each NEW one once. Which files were already
   relayed is remembered on the operator's machine; nothing on the owner's Mac is written or moved.

Output rules (cron delivers stdout to the operator's own alert channel, so silence = healthy):
- ONE alert line when a definite problem is seen on 2 checks in a row; ONE recovery line when it clears.
- FAILS CLOSED: a missing bot token or a token Telegram rejects (revoked/wrong) is a definite problem,
  not "healthy". If Telegram's status can't be read at all (network, Telegram error) the tick is
  UNKNOWN: it does not change strikes or the alert state, never prints a "recovered" line, and after
  3 unknown ticks in a row (15 min) it alerts that the status can't be read.
- ONE line per new relay request, every time, whatever the health state (at most 10 per tick).
  Each line is scrubbed again on the operator's machine: token-shaped strings, emails and phone
  numbers are removed before printing, whoever wrote the file.
The bot token stays on the owner's Mac and is never printed or copied.

Usage (cron on the operator's machine, one job per owner):
  stall_watch.py --name <short-name> --host <ssh user>@<tailscale ip or name>
Install (on the operator's machine, per owner; .sh scripts run via bash so no argument passing is needed):
  1. cp stall_watch.py ~/.hermes/scripts/stall_watch.py          (once)
  2. write ~/.hermes/scripts/<name>_stall.sh containing exactly:
       #!/bin/bash
       STALL_NAME=<name> STALL_HOST=<user>@<tailscale-ip> exec python3 "$HOME/.hermes/scripts/stall_watch.py"
  3. hermes cron create "*/5 * * * *" --script <name>_stall.sh --no-agent --name "<name> stall watch" --deliver <the operator's own alert channel>
  Test first by running the .sh by hand twice: it must print nothing when healthy (apart from any
  relay request lines the owner's agent has already written, once each).
  Offline test: python3 tests/test_stall_watch.py (fake ssh, no host contacted).

Profile note: if the owner's agent runs as a named profile, set STALL_HERMES_HOME on the job to
that profile's home on the owner's Mac (default ~/.hermes), and STALL_GATEWAY_LABEL to that
profile's launchd label (default ai.hermes.gateway; see `launchctl list | grep hermes` on their Mac).

Gateway check: read from launchd (`launchctl print gui/<uid>/<label>` says "state = running";
it names the GUI domain explicitly, so it works from an SSH session). A `pgrep -f "...gateway run..."`
check was not used because the launcher can put a long `python3 -I -c <script>` before
`gateway run`, which made that pattern report "down" while the gateway was running.
"""
import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path

UNKNOWN_LIMIT = 3       # unknown ticks in a row before "can't read their bot status"
RELAY_CHARS = 600       # longest request text printed per line
RELAYED_KEEP = 300      # how many relayed file names are remembered
RELAY_PER_TICK = 10     # at most this many request lines per tick; the rest go out on the next ticks

# Defence in depth: for_axiom.py already scrubs, but a request file may have been written
# another way, so every relayed line is scrubbed again here. Same secret list as for_axiom.py.
SECRET = "[secret removed]"
SECRET_RXES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), SECRET),
    (re.compile(r"(?i)([\"']?[\w.-]*(token|secret|password|passwd|api[_-]?key|apikey|private[_-]?key|"
                r"credential)[\w.-]*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|(?=[^\s,}]*\d)[^\s,}]{8,})"),
     r"\1" + SECRET),
    (re.compile(r"1//[\w-]{10,}"), SECRET),
    (re.compile(r"ya29\.[\w.-]{10,}"), SECRET),
    (re.compile(r"GOCSPX-[\w-]{4,}"), SECRET),
    (re.compile(r"\b\d{6,12}:[\w-]{30,}"), SECRET),
    (re.compile(r"AIza[\w-]{30,}"), SECRET),
    (re.compile(r"\b(sk|pk|rk)-[\w-]{16,}"), SECRET),
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[\w]{10,}"), SECRET),
    (re.compile(r"\bxox[abprs]-[\w-]{10,}"), SECRET),
    (re.compile(r"\bgh[pousr]_\w{20,}"), SECRET),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}"), SECRET),
]
LONG_RX = re.compile(r"[A-Za-z0-9_+=/-]{32,}")
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_RX = re.compile(r"\+?\d[\d\s().-]{6,}\d")
NAME_OK_RX = re.compile(r"^[\w.-]{1,80}$")


def _long(m):
    s = m.group(0)
    return SECRET if (re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s)) else s


def _phone(m):
    s = m.group(0)
    if re.match(r"\d{4}-\d{2}-\d{2}", s) and sum(c.isdigit() for c in s[10:]) < 7:
        return s
    return "[removed]" if sum(c.isdigit() for c in s) >= 10 else s


def scrub_line(text):
    for rx, rep in SECRET_RXES:
        text = rx.sub(rep, text)
    text = LONG_RX.sub(_long, text)
    text = EMAIL_RX.sub("[removed]", text)
    text = PHONE_RX.sub(_phone, text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())

REMOTE = r'''cd __HH__ || exit 3
set -a; . ./.env >/dev/null 2>&1; set +a
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then P=-2; else
P=$(curl -s -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(-1); raise SystemExit
if d.get('ok'):
    print(d.get('result',{}).get('pending_update_count',-1))
elif d.get('error_code') in (401,404):
    print(-3)
else:
    print(-1)")
fi
unset TELEGRAM_BOT_TOKEN
G=$(launchctl print "gui/$(id -u)/__LABEL__" 2>/dev/null | grep -q -E "^[[:space:]]*state = running" && echo up || echo down)
L=$(grep "inbound message: platform=telegram" logs/gateway.log 2>/dev/null | tail -1 | cut -c1-16)
echo "STATUS|$P|$G|$L"
find workspaces/FOR-AXIOM -maxdepth 1 -type f -name '*.md' -mtime -14 2>/dev/null | head -50 | while IFS= read -r f; do
  printf 'REQ|%s|%s\n' "$(basename "$f" | base64 | tr -d '\n')" "$(head -c 2000 "$f" | base64 | tr -d '\n')"
done'''


def remote_script(hermes_home, label="ai.hermes.gateway"):
    return REMOTE.replace("__HH__", hermes_home).replace("__LABEL__", label)


def _b64(s):
    try:
        return base64.b64decode(s.encode()).decode("utf-8", "replace")
    except Exception:
        return ""


def parse(stdout, rc):
    """-> (status tuple or None, [(name, text)], problem text when there is no status)"""
    status, reqs = None, []
    for line in (stdout or "").splitlines():
        if line.startswith("STATUS|"):
            pend, gw, last = (line.split("|", 3)[1:] + ["", "", ""])[:3]
            try:
                pend = int(pend)
            except ValueError:
                pend = -1
            status = (pend, gw.strip(), last.strip())
        elif line.startswith("REQ|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                name = _b64(parts[1]).strip()
                if name:
                    reqs.append((name, _b64(parts[2])))
    if status is None:
        if rc == 3:
            return None, reqs, "their Hermes folder was not found on their Mac (wrong STALL_HERMES_HOME?)"
        return None, reqs, "their Mac is unreachable over SSH (asleep, offline or Tailscale down)"
    return status, reqs, None


def assess(status, noconn):
    """-> ('ok'|'problem'|'unknown', reason)"""
    if status is None:
        return "problem", noconn
    pend, gw, _ = status
    if gw != "up":
        return "problem", "their Hermes gateway is not running"
    if pend == -2:
        return "problem", "no Telegram bot token in their .env, so the bot cannot work"
    if pend == -3:
        return "problem", "Telegram rejected their bot token (revoked or wrong)"
    if pend > 0:
        return "problem", f"{pend} Telegram message(s) waiting, not picked up by their agent"
    if pend < 0:
        return "unknown", "Telegram's status for their bot could not be read (network or Telegram error)"
    return "ok", None


def load_state(path):
    st = {"strikes": 0, "alerted": False, "unknown": 0, "relayed": []}
    try:
        st.update(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=os.environ.get("STALL_NAME"))
    ap.add_argument("--host", default=os.environ.get("STALL_HOST"))
    a = ap.parse_args()
    if not a.name or not a.host:
        print("stall_watch: --name and --host are required")
        raise SystemExit(2)
    hermes_home = os.environ.get("STALL_HERMES_HOME", "~/.hermes")
    label = os.environ.get("STALL_GATEWAY_LABEL", "ai.hermes.gateway")
    state = Path.home() / ".hermes" / "cache" / f"{a.name}_tg_stall.json"
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, remote_script(hermes_home, label)],
                           capture_output=True, text=True, timeout=60)
        stdout, rc = r.stdout, r.returncode
    except Exception:
        stdout, rc = "", 255
    status, reqs, noconn = parse(stdout, rc)
    kind, reason = assess(status, noconn)
    last = status[2] if status else "?"
    st = load_state(state)
    out = []

    # 1. Relay: every new request once, whatever the health state.
    seen = set(st["relayed"])
    sent = 0
    for name, text in sorted(reqs):
        if name in seen:
            continue
        if sent >= RELAY_PER_TICK:
            break  # not marked relayed: goes out on a later tick
        shown = name if NAME_OK_RX.match(name) else "[odd file name]"
        body = scrub_line(text)[:RELAY_CHARS] or "(empty or unreadable request file)"
        out.append(f"{a.name} FOR AXIOM ({shown}): {body}")
        st["relayed"].append(name)
        sent += 1
    st["relayed"] = st["relayed"][-RELAYED_KEEP:]

    # 2. Health, failing closed.
    if kind == "problem":
        st["strikes"] += 1
        st["unknown"] = 0
        if st["strikes"] >= 2 and not st["alerted"]:
            out.append(f"{a.name} Telegram STALL: {reason}. Last inbound from them: {last or 'unknown'}. "
                       "Usual fix: restart their Hermes gateway over SSH (a token problem needs the owner's "
                       "capture-secret step). Read-only alert; nothing touched.")
            st["alerted"] = True
    elif kind == "unknown":
        st["unknown"] += 1
        if st["unknown"] >= UNKNOWN_LIMIT and not st["alerted"]:
            out.append(f"{a.name} Telegram status UNKNOWN for {st['unknown']} checks in a row: {reason}. "
                       "Their bot may be down or the token revoked; check by hand. Read-only alert; nothing touched.")
            st["alerted"] = True
    else:
        if st["alerted"]:
            out.append(f"{a.name} Telegram recovered: no messages waiting, gateway up, token accepted.")
        st.update({"strikes": 0, "alerted": False, "unknown": 0})

    for line in out:
        print(line)
    state.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.with_suffix(".new")
    tmp.write_text(json.dumps(st))
    tmp.replace(state)


if __name__ == "__main__":
    main()
