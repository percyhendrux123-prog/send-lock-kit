#!/usr/bin/env python3
"""gmail_auth_readonly.py: the OWNER grants Gmail READ-ONLY access, once, at their own Mac.
(Not covered by the offline tests: it opens a real Google sign-in. It follows the same pattern
as selfsend.py --auth.)

Why this exists: the stock Hermes google-workspace setup.py asks for read + send + modify. The
Gmail alert watcher only needs to READ. Least access is the standing rule, so this asks for
gmail.readonly and nothing else, and writes google_token.json (mode 600).

Run by the owner, in Terminal on their Mac, with them at the screen (a browser window opens):
    python3 ~/.hermes/scripts/gmail_auth_readonly.py
Needs <HERMES_HOME>/google_client_secret.json (a Google OAuth client the operator places there)
and the Google libraries in Hermes' Python (google_auth_oauthlib and googleapiclient; the script
finds them the same way selfsend.py does, and the watcher needs the same libraries).
Pick the WORK Gmail account on the Google screen. The watcher refuses any other mailbox.
Prints no token. Exit 2 = something to fix (message says what).
"""
import glob
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
SECRET = HERMES / "google_client_secret.json"
TOKEN = HERMES / "google_token.json"
SCOPE = ["https://www.googleapis.com/auth/gmail.readonly"]


def libs():
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
        return True
    except ImportError:
        pass
    for py in sorted(glob.glob(str(HERMES / "installs/*/environments/*/venv/bin/python3")), key=os.path.getmtime, reverse=True):
        if os.path.realpath(py) == os.path.realpath(sys.executable):
            continue
        if os.system(f'"{py}" -c "import google_auth_oauthlib, googleapiclient" 2>/dev/null') == 0:
            os.execv(py, [py, __file__, *sys.argv[1:]])
    return False


def main():
    if not SECRET.exists():
        print("STOP: google_client_secret.json is not in " + str(HERMES) + ". Axiom puts it there (RUNBOOK step 15). Nothing was changed.")
        sys.exit(2)
    if not libs():
        print("STOP: the Google sign-in tools are not installed in Hermes' Python (RUNBOOK step 15a). Tell Axiom. Nothing was changed.")
        sys.exit(2)
    if TOKEN.exists():
        print("A Gmail token already exists. To redo it, tell Axiom (it backs the old one up first). Nothing was changed.")
        sys.exit(2)
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPE)
    c = flow.run_local_server(port=0, prompt="consent")
    TOKEN.write_text(c.to_json())
    os.chmod(TOKEN, 0o600)
    print("Done. Gmail READ-ONLY access is connected. You can close this window.")


if __name__ == "__main__":
    main()
