#!/usr/bin/env python3
"""Allow/block matrix for the send-lock plugin. Plain python, no pytest needed:

    python3 tests/test_send_lock.py

It builds a throwaway HERMES_HOME with a fake owner config, imports the plugin from
this repo, and asserts the legacy matrix (_legacy_classify) on a list of sample tool calls.
Nothing outside the temp folder is touched. To test an installed copy instead, point
SEND_LOCK_PATH at it:
SEND_LOCK_PATH=~/.hermes/plugins/send-lock/__init__.py python3 test_send_lock.py
Add a row here for every false positive or miss you fix.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

OWNER_TG = "70000001"      # fake ids: this test never uses a real owner value
OTHER_TG = "70000002"

tmp = tempfile.mkdtemp(prefix="sendlock-test-")
os.environ["HERMES_HOME"] = tmp
os.environ.pop("AXIOM_OWNER_CONFIG", None)
Path(tmp, "axiom-owner.json").write_text(json.dumps({
    "owner_name": "Test Owner", "telegram_chat_id": OWNER_TG, "axiom_contact": "the Axiom line",
}))
Path(tmp, "config.yaml").write_text(
    "platforms:\n  telegram:\n    enabled: true\n    home_channel:\n"
    "      platform: telegram\n      chat_id: '%s'\n      name: Test Owner\n" % OWNER_TG)

here = Path(__file__).resolve().parent.parent
path = os.environ.get("SEND_LOCK_PATH") or str(here / "plugins" / "send-lock" / "__init__.py")
spec = importlib.util.spec_from_file_location("send_lock_under_test", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# The kit keeps this legacy matrix for regression tests only, not as its policy.
# The default registered policy is exercised independently by test_one_door_plugin.
m.classify = m._legacy_classify

H = tmp  # stands in for the agent's HERMES_HOME in paths
T = "terminal"

def term(cmd):
    return (T, {"command": cmd})

# (label, tool, args, expected kind or None)
ALLOW = [
    ("read Gmail in Chrome", *term("open -a 'Google Chrome' https://mail.google.com/mail/u/0/#inbox"), None),
    ("Gmail search via gws", *term("gws gmail messages list --query 'from:carrier newer_than:2d'"), None),
    ("Gmail create draft", *term("python3 gmail_draft.py --create-draft --to a@example.test"), None),
    ("curl a web page", *term("curl -s https://example.test/page"), None),
    ("run selfsend (door 1)", *term("python3 ~/.hermes/scripts/selfsend.py --to owner@example.test --subject x --body-file d.txt --dry-run"), None),
    ("selfsend status", *term("python3 %s/scripts/selfsend.py --status" % H), None),
    ("read the plugin", *term("cat %s/plugins/send-lock/__init__.py" % H), None),
    ("read the owner config", *term("cat %s/axiom-owner.json" % H), None),
    ("kanban note", *term("hermes kanban comment t_abc 'text the owner a reminder'"), None),
    ("kanban create card", *term("hermes kanban create 'For Axiom: door request: x'"), None),
    ("hermes update --check", *term("hermes update --check"), None),
    ("git status in hermes-agent", *term("cd ~/.hermes/hermes-agent && git status"), None),
    ("webapp status", *term("hermes webapp --status"), None),
    ("imsg (open text door)", *term("imsg send --to 'Alex' --text 'hi'"), None),
    ("osascript Messages (open text door)", *term("osascript -e 'tell application \"Messages\" to send \"hi\" to buddy \"Alex\"'"), None),
    ("cron list", "cronjob", {"action": "list"}, None),
    ("cron run now", "cronjob", {"action": "run", "job_id": "abc"}, None),
    ("cron pause", "cronjob", {"action": "pause", "job_id": "abc"}, None),
    ("cron run, deliver local", "cronjob", {"action": "run", "deliver": "local"}, None),
    ("cron run, deliver origin", "cronjob", {"action": "run", "deliver": "origin"}, None),
    ("cron run, deliver telegram (home is owner)", "cronjob", {"action": "run", "deliver": "telegram"}, None),
    ("cron run, deliver telegram:<owner id>", "cronjob", {"action": "run", "deliver": "telegram:" + OWNER_TG}, None),
    ("write a normal workspace note", "write_file", {"path": "%s/workspaces/notes/draft.md" % H, "content": "hi"}, None),
    ("write a post draft", "write_file", {"path": "/tmp/post.txt", "content": "hello"}, None),
    ("write non-code file in scripts folder", "write_file", {"path": "%s/scripts/notes.txt" % H, "content": "x"}, None),
    ("read_file of a guard file", "read_file", {"path": "%s/plugins/send-lock/__init__.py" % H}, None),
    ("memory tool", "memory", {"action": "add", "content": "text the owner at 9"}, None),
    # update --check with the suffixes models add by habit
    ("update --check 2>&1", *term("hermes update --check 2>&1"), None),
    ("update --check 2>&1 | tail -20", *term("hermes update --check 2>&1 | tail -20"), None),
    ("update --check | head -n 5", *term("hermes update --check | head -n 5"), None),
    ("update --check 2>/dev/null", *term("hermes update --check 2>/dev/null"), None),
    # kanban notes that mention an update or a send
    ("kanban card body mentions update, 2>&1 | tail (t3)", *term(
        "hermes kanban create \"For Axiom: Hermes update request: update to latest version\" --body "
        "\"Owner asked to run 'hermes update' to get the latest version. Blocked by send-lock.\" 2>&1 | tail -5"), None),
    ("kanban comment mentions sendmail", *term("hermes kanban comment t_abc 'owner wants sendmail to a prospect'"), None),
    ("kanban with -p profile flag", *term("hermes -p work kanban comment t_abc 'note'"), None),
    # the operator relay path (for_axiom.py)
    ("for_axiom update-request (~ path)", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind update-request --what \"owner asked: hermes update to newest\""), None),
    ("for_axiom door-request mentions sendmail", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind door-request --what 'owner wants sendmail to new leads'"), None),
    ("for_axiom with HERMES_HOME path", *term(
        "python3 %s/scripts/for_axiom.py --kind problem --what 'gmail alerts stuck'" % H), None),
    ("for_axiom with $HOME path and 2>&1", *term(
        "python3 $HOME/.hermes/scripts/for_axiom.py --kind problem --what 'cron failing' 2>&1"), None),
    ("for_axiom --status", *term("python3 ~/.hermes/scripts/for_axiom.py --status"), None),
    ("for_axiom --what-file", *term("python3 ~/.hermes/scripts/for_axiom.py --kind update-request --what-file /tmp/req.txt"), None),
    # r2: was allowed in r1 (the review gap); only for_axiom.py may write this folder now.
    ("write a note into FOR-AXIOM folder", "write_file", {"path": "%s/workspaces/FOR-AXIOM/n.md" % H, "content": "x"}, "guard"),
    ("read for_axiom.py", *term("cat ~/.hermes/scripts/for_axiom.py"), None),
]

BLOCK = [
    ("send_message tool", "send_message", {"target": "x", "message": "hi"}, "text"),
    ("hermes send", *term("hermes send --to someone 'hi'"), "text"),
    ("twilio", *term("python3 -c \"from twilio.rest import Client\""), "text"),
    ("photon send", *term("photon chat send --to +x"), "text"),
    ("Mail.app send", *term("osascript -e 'tell application \"Mail\" to send newMessage'"), "email"),
    ("sendmail", *term("echo hi | sendmail someone@example.test"), "email"),
    ("python smtplib", *term("python3 -c 'import smtplib; smtplib.SMTP(\"x\")'"), "email"),
    ("Gmail API send", *term("curl -X POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send"), "email"),
    ("Gmail API client send", *term("python3 -c \"svc.users().messages().send(userId='me', body=b)\""), "email"),
    ("Gmail drafts send", *term("python3 -c \"svc.users().drafts().send(userId='me', body=b)\""), "email"),
    ("himalaya send", *term("himalaya message send < msg.txt"), "email"),
    ("gws gmail send", *term("gws gmail messages send --to a@example.test"), "email"),
    ("cron create via tool", "cronjob", {"action": "create", "schedule": "*/5 * * * *"}, "monitor"),
    ("cron update via tool", "cronjob", {"action": "update", "job_id": "abc"}, "monitor"),
    ("cron resume via tool", "cronjob", {"action": "resume", "job_id": "abc"}, "monitor"),
    ("cron remove via tool", "cronjob", {"action": "remove", "job_id": "abc"}, "monitor"),
    ("cron create via CLI", *term("hermes cron create '*/5 * * * *' 'check mail'"), "monitor"),
    ("cron edit via CLI", *term("hermes -p x cron edit abc --schedule 'every 1m'"), "monitor"),
    ("launchctl load", *term("launchctl load ~/Library/LaunchAgents/x.plist"), "monitor"),
    ("write monitor script", "write_file", {"path": "%s/scripts/watch_new.py" % H, "content": "x"}, "monitor"),
    ("write a LaunchAgent plist", "write_file", {"path": os.path.expanduser("~/Library/LaunchAgents/x.plist"), "content": "x"}, "monitor"),
    ("write into work-gmail-watch dir", "write_file", {"path": "%s/workspaces/work-gmail-watch/tick.sh" % H, "content": "x"}, "monitor"),
    ("cron deliver to a group (run)", "cronjob", {"action": "run", "deliver": "telegram:-100099"}, "schedule"),
    ("cron deliver to another user id", "cronjob", {"action": "run", "deliver": "telegram:" + OTHER_TG}, "schedule"),
    ("cron deliver mixed list", "cronjob", {"action": "run", "deliver": "telegram,photon"}, "schedule"),
    ("cron deliver discord", "cronjob", {"action": "run", "deliver": "discord"}, "schedule"),
    ("hermes update", *term("hermes update"), "upgrade"),
    ("hermes update with profile flag", *term("hermes -p x update"), "upgrade"),
    ("hermes update --check chained", *term("hermes update --check && hermes update"), "upgrade"),
    ("git pull in hermes-agent", *term("cd ~/.hermes/hermes-agent && git pull"), "upgrade"),
    ("git checkout main in hermes-agent", *term("git -C ~/.hermes/hermes-agent checkout main"), "upgrade"),
    ("enable webapp", *term("hermes config set webapp.enabled true"), "upgrade"),
    ("hermes webapp start", *term("hermes webapp start"), "upgrade"),
    ("write into hermes-agent", "write_file", {"path": os.path.expanduser("~/.hermes/hermes-agent/run.py"), "content": "x"}, "upgrade"),
    ("patch the plugin", "patch", {"path": "%s/plugins/send-lock/__init__.py" % H, "old_string": "a", "new_string": "b"}, "guard"),
    ("write selfsend.py", "write_file", {"path": "%s/scripts/selfsend.py" % H, "content": "x"}, "monitor"),  # scripts-folder code is caught as monitor first; either way it is blocked
    ("write the owner config", "write_file", {"path": "%s/axiom-owner.json" % H, "content": "{}"}, "guard"),
    ("write the send token", "write_file", {"path": "%s/google_token_send.json" % H, "content": "{}"}, "guard"),
    ("shell: overwrite plugin", *term("cp /tmp/x.py %s/plugins/send-lock/__init__.py" % H), "guard"),
    ("shell: sed -i the plugin", *term("sed -i '' 's/a/b/' ~/.hermes/plugins/send-lock/__init__.py"), "guard"),
    ("shell: redirect into owner config", *term("echo '{}' > %s/axiom-owner.json" % H), "guard"),
    ("shell: read the send token", *term("cat ~/.hermes/google_token_send.json"), "guard"),
    # the relaxed suffixes and the plain-command shortcut must not open a hole
    ("update --check then update (;)", *term("hermes update --check 2>&1; hermes update"), "upgrade"),
    ("update --check | tail && update", *term("hermes update --check | tail -5 && hermes update"), "upgrade"),
    ("update --check piped to sh", *term("hermes update --check | sh"), "upgrade"),
    ("kanban chained with update", *term("hermes kanban create 'x' && hermes update"), "upgrade"),
    ("kanban + newline + update", *term("hermes kanban comment t_abc 'x'\nhermes update"), "upgrade"),
    ("kanban with $( ) update", *term("hermes kanban create \"$(hermes update)\""), "upgrade"),
    ("kanban with backticks", *term("hermes kanban create \"`hermes update`\""), "upgrade"),
    ("kanban unbalanced quote", *term("hermes kanban comment t_abc 'hermes update"), "upgrade"),
    ("kanban piped to sendmail", *term("hermes kanban show t_abc | sendmail a@example.test"), "email"),
    ("for_axiom chained with sendmail", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what x; echo hi | sendmail a@example.test"), "email"),
    ("for_axiom piped to sendmail", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind other --what x | sendmail a@example.test"), "email"),
    ("for_axiom + newline + update", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind other --what x\nhermes update"), "upgrade"),
    ("for_axiom with $( )", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind other --what \"$(hermes update)\""), "upgrade"),
    ("lookalike for_axiom.py in /tmp", *term("python3 /tmp/for_axiom.py --what 'hermes update'"), "upgrade"),
    ("lookalike scripts/for_axiom.py outside Hermes home", *term("python3 /tmp/scripts/for_axiom.py --what 'hermes update'"), "upgrade"),
    ("lookalike for_axiom.py with smtplib", *term("python3 /tmp/x/for_axiom.py --what 'import smtplib'"), "email"),
    ("write for_axiom.py", "write_file", {"path": "%s/scripts/for_axiom.py" % H, "content": "x"}, "monitor"),
    ("shell: overwrite for_axiom.py", *term("cp /tmp/x.py ~/.hermes/scripts/for_axiom.py"), "guard"),
    ("shell: sed -i for_axiom.py", *term("sed -i '' 's/a/b/' %s/scripts/for_axiom.py" % H), "guard"),
    # A request for the operator relay may never carry a secret file.
    ("for_axiom --what-file send token (~)", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what-file ~/.hermes/google_token_send.json"), "guard"),
    ("for_axiom --what-file send token (HH)", *term(
        "python3 %s/scripts/for_axiom.py --kind problem --what-file %s/google_token_send.json" % (H, H)), "guard"),
    ("for_axiom --what-file read token", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what-file ~/.hermes/google_token.json"), "guard"),
    ("for_axiom --what-file .env", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what-file ~/.hermes/.env"), "guard"),
    ("for_axiom --what-file .env + 2>&1", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what-file $HOME/.hermes/.env 2>&1"), "guard"),
    ("for_axiom --what-file client_secret", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind other --what-file ~/Downloads/client_secret_123.json"), "guard"),
    ("for_axiom --what-file owner config", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind other --what-file %s/axiom-owner.json" % H), "guard"),
    ("for_axiom --what-file plugin code", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind other --what-file ~/.hermes/plugins/send-lock/__init__.py"), "guard"),
    ("for_axiom --what-file ssh key", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind other --what-file ~/.ssh/id_ed25519"), "guard"),
    ("for_axiom --what names .env", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what 'the .env has no bot token'"), "guard"),
    ("for_axiom non-plain + token file", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what-file ~/.hermes/google_token.json; ls"), "guard"),
    ("lookalike for_axiom + .env", *term("python3 /tmp/for_axiom.py --what-file ~/.hermes/.env"), "guard"),
    ("kanban note naming .env", *term("hermes kanban comment t1 'see ~/.hermes/.env'"), "guard"),
    ("for_axiom plain words about the token", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what 'the Gmail token is refused since 9am'"), None),
    ("for_axiom --what-file /tmp text", *term(
        "python3 ~/.hermes/scripts/for_axiom.py --kind problem --what-file /tmp/for-axiom.txt"), None),
    # r2: only for_axiom.py writes the FOR-AXIOM request folder (scrub + daily cap).
    ("write_file into FOR-AXIOM", "write_file",
     {"path": "%s/workspaces/FOR-AXIOM/20261027-090000-problem.md" % H, "content": "x"}, "guard"),
    ("write_file into FOR-AXIOM (~)", "write_file",
     {"path": "~/.hermes/workspaces/FOR-AXIOM/x.md", "content": "x"}, "guard"),
    ("patch a FOR-AXIOM file", "patch",
     {"path": "%s/workspaces/FOR-AXIOM/x.md" % H, "old_string": "a", "new_string": "b"}, "guard"),
    ("shell: redirect into FOR-AXIOM", *term("echo hi > ~/.hermes/workspaces/FOR-AXIOM/x.md"), "guard"),
    ("shell: cp a file into FOR-AXIOM", *term("cp /tmp/x.md %s/workspaces/FOR-AXIOM/" % H), "guard"),
    ("shell: python open() into FOR-AXIOM", *term(
        "python3 -c \"open('%s/workspaces/FOR-AXIOM/x.md','w').write('x')\"" % H), "guard"),
    ("shell: read FOR-AXIOM", *term("ls -lt ~/.hermes/workspaces/FOR-AXIOM"), None),
    ("write_file /tmp text for --what-file", "write_file", {"path": "/tmp/for-axiom.txt", "content": "x"}, None),
]

passed = failed = 0
for label, tool, args, want in ALLOW + BLOCK:
    got = m.classify(tool, args)
    if got == want:
        passed += 1
    else:
        failed += 1
        print("FAIL  %-48s want=%r got=%r" % (label, want, got))

# Fail-closed checks: no owner config -> owner-only delivery must be refused.
Path(tmp, "axiom-owner.json").unlink()
for label, args in [("no owner config: telegram home", {"action": "run", "deliver": "telegram"}),
                    ("no owner config: telegram:<id>", {"action": "run", "deliver": "telegram:" + OWNER_TG})]:
    got = m.classify("cronjob", args)
    if got == "schedule":
        passed += 1
    else:
        failed += 1
        print("FAIL  %-48s want='schedule' got=%r" % (label, got))

# Block message has no unfilled placeholder and still names a way forward.
Path(tmp, "axiom-owner.json").write_text(json.dumps({"owner_name": "Test Owner", "telegram_chat_id": OWNER_TG,
                                                     "axiom_contact": "the Axiom line"}))
for kind in m.WHY:
    msg = m._fmt(kind)
    handoff = ("Tell the owner/operator in the current conversation" in msg and "for_axiom.py" not in msg
               if kind == 'policy' else "scripts/for_axiom.py --kind" in msg and "kanban card on this Mac does not reach Axiom" in msg)
    ok = ("{" not in msg and "BLOCKED by Axiom send-lock" in msg and "Test Owner" in msg and handoff)
    if ok:
        passed += 1
    else:
        failed += 1
        print("FAIL  message for kind %r has a placeholder, no owner name, or no for_axiom path" % kind)

# Named profile: an agent profile home may be written with "~". Import a second copy of
# the plugin with HERMES_HOME set to a (non-existent, never written) profile home under the user's home.
prof = os.path.expanduser("~/.hermes/profiles/sendlock-test-nonexistent")
os.environ["HERMES_HOME"] = prof
spec2 = importlib.util.spec_from_file_location("send_lock_under_test_profile", path)
m2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(m2)
m2.classify = m2._legacy_classify
for label, cmd, want in [
    ("profile: for_axiom via ~ profile path", "python3 ~/.hermes/profiles/sendlock-test-nonexistent/scripts/for_axiom.py "
     "--kind update-request --what 'owner asked for hermes update'", None),
    ("profile: for_axiom via absolute profile path", "python3 %s/scripts/for_axiom.py --kind problem --what 'hermes update'" % prof, None),
    ("profile: other profile's for_axiom.py", "python3 ~/.hermes/profiles/other/scripts/for_axiom.py --what 'hermes update'", "upgrade"),
]:
    got = m2.classify("terminal", {"command": cmd})
    if got == want:
        passed += 1
    else:
        failed += 1
        print("FAIL  %-48s want=%r got=%r" % (label, want, got))
os.environ["HERMES_HOME"] = tmp

# Multiplexed gateway: inside it HERMES_HOME is the DEFAULT home and Hermes knows the
# profile through a context-local override. Simulate that with a stand-in hermes_constants module on the
# FIRST plugin copy (imported with HERMES_HOME=tmp): the home must follow the override at call time.
import types
fake = types.ModuleType("hermes_constants")
fake.get_hermes_home = lambda: Path(prof)
sys.modules["hermes_constants"] = fake
for label, tool, args, want in [
    ("multiplexer: for_axiom in the calling profile", *term(
        "python3 ~/.hermes/profiles/sendlock-test-nonexistent/scripts/for_axiom.py --kind update-request --what 'hermes update'"), None),
    ("multiplexer: owner config of the calling profile is a guard", "write_file",
     {"path": prof + "/axiom-owner.json", "content": "{}"}, "guard"),
    ("multiplexer: monitor code in the calling profile's scripts", "write_file",
     {"path": prof + "/scripts/watch.py", "content": "x"}, "monitor"),
]:
    got = m.classify(tool, args)
    if got == want:
        passed += 1
    else:
        failed += 1
        print("FAIL  %-48s want=%r got=%r" % (label, want, got))
check_home = getattr(m, "_hh", lambda: None)() == prof and m._fmt("upgrade").count("~/.hermes/profiles/sendlock-test-nonexistent/scripts/for_axiom.py") == 1
passed, failed = (passed + 1, failed) if check_home else (passed, failed + 1)
if not check_home:
    print("FAIL  multiplexer: _hh() and the block message follow the calling profile")
del sys.modules["hermes_constants"]

print("send-lock %s: %d passed, %d failed" % (m.VERSION, passed, failed))
sys.exit(1 if failed else 0)
