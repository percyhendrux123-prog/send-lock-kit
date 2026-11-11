"""send-lock: a default-deny pre_tool_call hook for Hermes.

What the hook does today (classify() / pre_tool_call()):
  - Allows a short explicit list: read-only tools (read_file, search_files, skill_view,
    skills_list, todo, todo_list, text_to_speech), the terminal commands `ls`, `pwd`,
    `whoami` and a plain `curl -q -s <URL>` GET, and write_file in two places only:
    fixture notes under /workspace/fixtures at step 0, and /outbox/<id>.json drafts at
    step 2 or above.
  - Blocks every other tool and command, including wrappers and unknown tools, with a
    plain-English message. A hook error also returns a block, never an allow.
  - Allows `imsg send` only at step 3 or above and only if the owner config sets
    allow_imessage to true.

Owner values are read at call time from <HERMES_HOME>/axiom-owner.json, or from the file
named by the environment variable AXIOM_OWNER_CONFIG. The file name is kept for
compatibility. Keys the hook reads: step (integer 0-4), allow_imessage (true/false),
owner_name and axiom_contact (shown in block messages). A missing, unreadable or invalid
file means step 0.

Side effects: reads the owner config; appends one line per refusal (tool name and kind, never
the arguments) to <HERMES_HOME>/logs/send-lock.log. No network, subprocess or model calls.

This hook alone is not a containment boundary. It is a friendly first layer that tells the
agent why something was refused. In the worker-in-wall test, 14 of 46 attack cases passed
this hook and were stopped by the container wall (see docs/THREAT-LIST.md).

_legacy_classify() and the code around it are an older, larger rule matrix. It is kept only
so tests/test_send_lock.py can still run its regression cases. It is never registered, never
a fallback, and not part of the policy.
"""
import functools
import json
import os
import re
from datetime import datetime
from pathlib import Path

VERSION = "kit-2.0"
HOME = str(Path.home())
HH = str(Path(os.environ.get("HERMES_HOME") or (HOME + "/.hermes")))  # import-time value; see _hh()


def _hh():
    """The Hermes home of the agent making THIS call. Inside a multiplexed gateway one
    process serves several profiles and HERMES_HOME is the default home, so ask Hermes for the
    context-local home first, then the env var at call time, then ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home
        h = str(get_hermes_home() or "")
        if h:
            return str(Path(h))
    except Exception:
        pass
    return str(Path(os.environ.get("HERMES_HOME") or (HOME + "/.hermes")))


def _log_path():
    return Path(_hh()) / "logs" / "send-lock.log"


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate owner config key')
        out[key] = value
    return out


def _owner_cfg():
    """Read the owner config at call time. Never cached, never logged. {} on any problem."""
    p = os.environ.get("AXIOM_OWNER_CONFIG") or (_hh() + "/axiom-owner.json")
    try:
        d = json.loads(Path(p).read_text(), object_pairs_hook=_unique_object)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _owner_tg():
    v = str(_owner_cfg().get("telegram_chat_id") or "").strip()
    return v if re.fullmatch(r"-?\d+", v) else ""


def _home_display():
    h = _hh()
    return "~" + h[len(HOME):] if h == HOME or h.startswith(HOME + "/") else h


def _fmt(kind):
    c = _owner_cfg()
    reach = ('\nTell the owner/operator in the current conversation what was refused. '
             'They must contact Axiom through their own approved channel. '
             'This kit has no automatic escalation; do not run a helper or bypass the refusal.'
             if kind == 'policy' else REACH_AXIOM)
    return (WHY[kind] + reach).format(
        v=VERSION,
        owner=str(c.get("owner_name") or "the owner"),
        axiom=str(c.get("axiom_contact") or "Axiom"),
        fa=_home_display() + "/scripts/for_axiom.py",
    )


# Legacy matrix only (see the module docstring): the way to reach the operator relay.
REACH_AXIOM = (
    "\nHow to reach Axiom: python3 {fa} --kind <door-request|update-request|problem> --what \"<one line>\" "
    "(Axiom's watcher picks it up within about 5 minutes while this Mac is online). A kanban card on "
    "this Mac does not reach Axiom."
)


# One explanation per kind of block. Plain English, written for the owner.
WHY = {
    "text": (
        "BLOCKED by Axiom send-lock ({v}). KIND: PERMISSION (a door that is closed on purpose, "
        "not a missing feature).\n"
        "What was stopped: a text or iMessage to someone through a service that is not an open door.\n"
        "Why: texts to leads fall under state and federal texting rules (consent, STOP, texting hours). "
        "Those checks are not switched on for this service.\n"
        "What still works: iMessage/SMS from this Mac's Messages app when {owner} asks for it in chat.\n"
        "What opens it: a new door, {owner}'s OK and Axiom's OK."
    ),
    "email": (
        "BLOCKED by Axiom send-lock ({v}). KIND: PERMISSION (a door that is closed on purpose, "
        "not a missing feature).\n"
        "What was stopped: an email going out to someone other than {owner}.\n"
        "Why: email from this address goes out under {owner}'s license, so every outside email waits for "
        "{owner}'s OK until the agent has a clean track record.\n"
        "What still works: read and search Gmail, prepare the full email as a preview, and send it to "
        "{owner} alone with the self-send script (Door 1: only {owner}'s own addresses).\n"
        "What opens it: Door 2 (send to others after {owner} approves each email), {owner}'s OK and Axiom's OK. No cost."
    ),
    "schedule": (
        "BLOCKED by Axiom send-lock ({v}). KIND: PERMISSION.\n"
        "What was stopped: a scheduled job that delivers a message somewhere other than this chat.\n"
        "Why: a scheduled message would go out with nobody looking at it.\n"
        "What still works: scheduled jobs that report back into this chat (deliver: local or origin), "
        "or to {owner}'s own private Telegram chat (deliver: telegram, Door A).\n"
        "What opens it: a later door, {owner}'s OK and Axiom's OK."
    ),
    "upgrade": (
        "BLOCKED by Axiom send-lock ({v}). KIND: PERMISSION (a door that is closed on purpose, "
        "not a missing feature).\n"
        "What was stopped: updating Hermes, switching it to unreleased code, turning on the Webapp, "
        "or changing the Hermes program files.\n"
        "Why: this Mac carries Axiom's setup that keeps the alerts delivering. An update "
        "can overwrite it and quietly break the alerts. Risk: HIGH.\n"
        "What still works: checking whether an update exists (hermes update --check). Tell {owner} what's new.\n"
        "What opens it: the operator does Hermes updates after testing. Send the request with "
        "for_axiom.py --kind update-request (below) and tell {owner} it's with Axiom."
    ),
    "guard": (
        "BLOCKED by Axiom send-lock ({v}). KIND: PERMISSION.\n"
        "What was stopped: a change to the safety files themselves (the send-lock, its config, selfsend.py, "
        "for_axiom.py or the FOR-AXIOM request folder), or a request for Axiom that names a secret file "
        "(a token, the .env, the owner config, the lock's code).\n"
        "Why: if the agent could edit its own limits, the limits wouldn't mean anything, and a secret must "
        "never travel in a message, not even to Axiom.\n"
        "What still works: describe the problem in plain words (\"the Gmail token is refused\"), without the "
        "file's name or contents. Only for_axiom.py writes into the FOR-AXIOM folder.\n"
        "What opens it: Axiom changes these files after {owner} and Axiom agree. Ask {owner} to tell Axiom what's needed."
    ),
    "monitor": (
        "BLOCKED by Axiom send-lock ({v}). KIND: PERMISSION (a door that is closed on purpose, "
        "not a missing feature).\n"
        "What was stopped: creating or changing a scheduled job, alert or monitor (cron jobs, monitor "
        "scripts, background services).\n"
        "Why: Axiom runs and fixes {owner}'s alerts and monitors from its side, so they stay steady and "
        "{owner} isn't spending the agent's time on plumbing.\n"
        "What still works: list jobs, run one now, read their output, and pause a job that is failing "
        "or spamming {owner}.\n"
        "What opens it: tell {owner} plainly what you see and send Axiom what is wrong with "
        "for_axiom.py --kind problem (below). {owner} can also reach Axiom at {axiom}. Axiom fixes it."
    ),
}

# ---- Legacy rule matrix: constants and helpers used only by _legacy_classify() (regression tests) ----
CONTENT_TOOLS = {
    "write_file", "patch", "read_file", "search_files", "memory", "context_notes",
    "skill_manage", "skill_view", "skills_list", "todo", "todo_list", "vision_analyze",
    "session_search", "chat_history_lookup", "clarify",
}
WRITE_TOOLS = {"write_file", "patch"}
ALWAYS_BLOCK = {"send_message": "text", "text_to_message": "text"}

# Guard files: the agent may read them, never change them.
_GUARD_REL = ["plugins/send-lock", "scripts/selfsend.py", "scripts/for_axiom.py", "google_token_send.json",
              "axiom-owner.json",
              # Only for_axiom.py writes the request folder (it scrubs and caps); the agent may
              # read it, never write, copy, move or delete in it with a tool or the shell.
              "FOR-AXIOM"]
# Secret files. The two door commands that skip the usual checks (a plain kanban note and a
# plain for_axiom.py run) may not NAME any of these, so a request can never carry a token, the .env, the
# owner config or the lock's own code. Describe the problem in words instead.
SECRET_NAME_RX = re.compile(
    r"(\.env\b|google_token|client_secret|credentials\.json|axiom-owner|auth\.json|\bplugins/|\.ssh\b|"
    r"\bid_(rsa|ed25519|ecdsa)\b|\.netrc\b|\bkeychains?\b|\.(pem|p12|key)\b|token\.json\b)",
    re.I)


@functools.lru_cache(maxsize=16)
def _guard_rx(hh):
    paths = []
    for rel in _GUARD_REL:
        paths += [hh + "/" + rel, HOME + "/.hermes/" + rel, "~/.hermes/" + rel, rel]
    return re.compile("|".join(re.escape(x) for x in dict.fromkeys(paths)))
# Shell verbs that change a file (reading the guard files is fine).
SHELL_WRITE_RX = re.compile(
    r"(>|\btee\b|\bsed\s+-i|\bperl\s+-p?i|\bcp\b|\bmv\b|\brm\b|\bchmod\b|\bchown\b|\bln\b|\btruncate\b|"
    r"\bdd\b|\binstall\b|open\([^)]*['\"][wa+])"
)

TEXT_PATTERNS = [
    # Door iMessage/SMS is OPEN. Native Messages sends from this Mac
    # (osascript "Messages", imsg, sms:/imessage: links) are not blocked; each one is
    # logged as "text-door" below. Other text services stay closed.
    r"\bhermes\s+send\b", r"\bsend_message\b",
    r"photon[^\n]{0,120}\b(send|message|chat)\b", r"api\.photon", r"\bspectrum\b[^\n]{0,60}\bsend\b",
    r"\btwilio\b",
]
DOOR_TEXT_RX = re.compile("|".join([
    r"\bimsg\b", r"tell\s+application\s+[\\\"']*messages\b",
    r"\b(to\s+buddy|to\s+participant)\b", r"\bsms:", r"\bimessage:",
]), re.I)
EMAIL_PATTERNS = [
    r"tell\s+application\s+[\\\"']*mail\b",
    r"\bsendmail\b", r"\bmail\s+-s\b", r"\bmsmtp\b", r"\bswaks\b", r"smtplib",
    r"himalaya[^\n]{0,120}\b(send|write|reply|forward)\b", r"\bmessage\s+send\b",
    r"(messages|drafts)\(\)\s*\.\s*send\s*\(", r"users\.(messages|drafts)\.send",
    r"/gmail/v1/users/[^\s\"']*/(messages|drafts)/send",
    r"\bgws\b[^\n]{0,80}\bsend\b", r"\bgmail\b[^\n]{0,40}\bsend\b\s*(--|-[a-z]|\()",
]
TEXT_RX = re.compile("|".join(TEXT_PATTERNS), re.I)
EMAIL_RX = re.compile("|".join(EMAIL_PATTERNS), re.I)

# upgrade hold
HA_RX = re.compile(r"hermes-agent")
GIT_MUTATE_RX = re.compile(r"\bgit\b[^\n;&|]{0,160}\b(pull|checkout|switch|reset|stash|merge|rebase|clean|restore|apply|am|cherry-pick|commit|push)\b")
UPDATE_RX = re.compile(r"\bhermes(\s+-p\s+\S+)?\s+update\b")
UPDATE_READONLY_RX = re.compile(r"^\s*hermes(\s+-p\s+\S+)?\s+update\s+(--check|--plan|--help|-h)(\s+--json)?\s*$")
# Suffixes models add by habit. Stripped once, at the very end, before the plain/read-only checks.
BENIGN_TAIL_RX = re.compile(r"(\s+2>(&1|/dev/null))?(\s*\|\s*(tail|head)(\s+-n)?(\s+-?\d+)?)?\s*$")
KANBAN_RX = re.compile(r"^\s*hermes(\s+-p\s+\S+)?\s+kanban\s+(comment|create|show|list|block|unblock|complete)\b")
def _home_forms(h):
    """The same Hermes home written the ways a command line may write it (absolute, ~, $HOME)."""
    out = [h]
    if h == HOME or h.startswith(HOME + "/"):
        rest = h[len(HOME):]
        out += ["~" + rest, "$HOME" + rest, "${HOME}" + rest]
    return out


@functools.lru_cache(maxsize=16)
def _for_axiom_rx(hh):
    homes = dict.fromkeys(_home_forms(hh) + _home_forms(HOME + "/.hermes"))
    return re.compile(
        r"^\s*(python3?(\.\d+)?|/usr/bin/python3|/opt/homebrew/bin/python3)\s+(%s)/scripts/for_axiom\.py(\s|$)"
        % "|".join(re.escape(h) for h in homes))
WEBAPP_RX = re.compile(r"webapp\.(enabled|host|public_url|port)|\bbuild-webapp\b|\bhermes(\s+-p\s+\S+)?\s+webapp\b(?!\s+--status)")
HA_DIR = HOME + "/.hermes/hermes-agent"

# monitor freeze
CRON_OK_ACTIONS = {"list", "run", "pause", "status", ""}
MON_DIRS = ("/.hermes/scripts/", "/workspaces/work-gmail-watch/", "/Library/LaunchAgents/")  # + <home>/scripts/ at call time
MON_EXT_RX = re.compile(r"\.(py|sh|plist|command)$")
CRON_CLI_RX = re.compile(r"\bhermes(\s+-p\s+\S+)?\s+cron\s+(create|add|new|edit|update|remove|rm|delete|resume|enable)\b")
LAUNCHCTL_RX = re.compile(r"\blaunchctl\s+(load|bootstrap|enable|submit)\b")


def _monitor_write(a):
    t = str(Path(str(a.get("path") or a.get("file_path") or "")).expanduser())
    if "/Library/LaunchAgents/" in t:
        return True
    dirs = list(MON_DIRS) + [_hh() + "/scripts/"]
    extra = _owner_cfg().get("extra_monitor_dirs")
    if isinstance(extra, list):
        dirs += [str(x) for x in extra if x]
    return any(d in t for d in dirs) and bool(MON_EXT_RX.search(t))


def _upgrade(tool_name, a, cmd, blob):
    if tool_name in WRITE_TOOLS:
        target = str(Path(str(a.get("path") or a.get("file_path") or "")).expanduser())
        return target.startswith(HA_DIR) or "/.hermes/hermes-agent/" in target
    if UPDATE_RX.search(blob) and not UPDATE_READONLY_RX.match(_strip_benign_tail(cmd)):
        return True
    if HA_RX.search(blob) and GIT_MUTATE_RX.search(blob):
        return True
    if WEBAPP_RX.search(blob):
        return True
    return False


def _log(tool, kind, snippet):
    try:
        log = _log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{VERSION}\t{kind}\t{tool}\t{snippet[:400]!r}\n")
    except OSError:
        pass


def _strip_benign_tail(cmd):
    """Drop one trailing `2>&1` / `2>/dev/null` and one `| tail -N` / `| head -N`."""
    m = BENIGN_TAIL_RX.search(cmd)
    return cmd[:m.start()] if m else cmd


def _unquoted_ops(cmd):
    """True if cmd has a shell operator or a newline outside quotes, or an unbalanced quote."""
    q = None
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            if c == q:
                q = None
            elif c == "\\" and q == '"':
                i += 1
        elif c in "'\"":
            q = c
        elif c == "\\":
            i += 1
        elif c in ";&|<>()\n\r":
            return True
        i += 1
    return q is not None


def _is_plain(cmd, head_rx):
    """One command, no chaining, no substitution, matching head_rx (after the habit suffixes)."""
    core = _strip_benign_tail(cmd)
    if not head_rx.match(core):
        return False
    if "$(" in core or "`" in core:
        return False
    return not _unquoted_ops(core)


def _is_plain_kanban(cmd):
    """A lone `hermes kanban ...` command is a note, never a send. It stays on this Mac."""
    return _is_plain(cmd, KANBAN_RX)


def _is_plain_for_axiom(cmd):
    """A lone run of the installed for_axiom.py: it only writes one request file for Axiom's watcher."""
    return _is_plain(cmd, _for_axiom_rx(_hh()))


def _tg_home_is_owner():
    """True only if config.yaml's telegram home_channel chat_id is the owner's own DM
    (the id in axiom-owner.json). Fails closed when either side is missing."""
    owner = _owner_tg()
    if not owner:
        return False
    try:
        txt = (Path(_hh()) / "config.yaml").read_text()
    except OSError:
        return False
    ids = re.findall(r"home_channel:\s*\n(?:[ \t]+.*\n){0,6}?[ \t]+chat_id:\s*['\"]?(-?\d+)", txt)
    return bool(ids) and all(i == owner for i in ids)


def _owner_only(deliver):
    """Door A: scheduled alerts may go to the owner's own private Telegram chat only.
    Mixed lists (e.g. 'telegram,photon') still block."""
    owner = _owner_tg()
    targets = [t.strip() for t in deliver.split(",") if t.strip()]
    if not targets:
        return False
    for t in targets:
        tl = t.lower()
        if tl in ("local", "origin", "none"):
            continue
        if tl == "telegram" and _tg_home_is_owner():
            continue
        if owner and tl == "telegram:" + owner:
            continue
        return False
    return True


def _legacy_classify(tool_name, args):
    """Legacy rule matrix, kept only for regression tests. Never registered as the policy hook."""
    a = args or {}
    if tool_name in ALWAYS_BLOCK:
        return ALWAYS_BLOCK[tool_name]
    if tool_name in ("cronjob", "cronjob_manage", "cron"):
        if str(a.get("action") or "").strip().lower() not in CRON_OK_ACTIONS:
            return "monitor"
        deliver = str(a.get("deliver") or a.get("delivery") or "")
        if deliver and deliver.lower() not in ("local", "origin", "none", "") and not _owner_only(deliver):
            return "schedule"
        return None
    if tool_name in WRITE_TOOLS and _upgrade(tool_name, a, "", ""):
        return "upgrade"
    if tool_name in WRITE_TOOLS and _monitor_write(a):
        return "monitor"
    if tool_name in WRITE_TOOLS:
        target = str(a.get("path") or a.get("file_path") or "")
        grx = _guard_rx(_hh())
        if grx.search(target) or grx.search(str(Path(target).expanduser())):
            return "guard"
        return None
    if tool_name in CONTENT_TOOLS:
        return None
    cmd = str(a.get("command") or "")
    if _is_plain_kanban(cmd) or _is_plain_for_axiom(cmd):
        # The door commands skip the send checks, never the secret-file check.
        return "guard" if SECRET_NAME_RX.search(cmd) else None
    blob = json.dumps(a, default=str).replace("\\n", "\n")
    # Any for_axiom.py run (plain or not, real or lookalike) that names a secret file is a guard block.
    if "for_axiom" in blob and SECRET_NAME_RX.search(blob):
        return "guard"
    # Running selfsend.py is allowed; changing it, or touching the send token, is not.
    # Same for for_axiom.py.
    stripped = re.sub(r"python3?\S*\s+\S*(selfsend|for_axiom)\.py", "DOORSCRIPT", blob)
    if "google_token_send" in stripped:
        return "guard"
    if _guard_rx(_hh()).search(stripped) and SHELL_WRITE_RX.search(stripped):
        return "guard"
    if _upgrade(tool_name, a, cmd, blob):
        return "upgrade"
    if CRON_CLI_RX.search(blob) or LAUNCHCTL_RX.search(blob):
        return "monitor"
    if TEXT_RX.search(blob):
        return "text"
    if EMAIL_RX.search(blob):
        return "email"
    return None


# ---- Kit policy: what the registered hook uses ----
# Kit policy: deliberately small. The legacy classifier above exists for regression tests,
# NOT as a fallback. OS mounts and the network wall are the real boundary.
KIT_READ_TOOLS = frozenset({'read_file', 'search_files', 'skill_view', 'skills_list',
                            'todo', 'todo_list', 'text_to_speech'})
WHY['policy'] = (
    'BLOCKED by Axiom send-lock ({v}). KIND: PERMISSION.\n'
    'This tool or command is not on {owner}\'s explicit kit allowlist. '
    'Prepare a draft in /outbox at step 2 or above; only the separate sender '
    'can deliver it after an owner approval. Missing owner config means step 0. '
    'Axiom must review tool opt-ins; this hook alone is not a network boundary.'
)


def _step():
    value = _owner_cfg().get('step', 0)
    return value if type(value) is int and 0 <= value <= 4 else 0


def _kit_terminal(cmd):
    import shlex
    from urllib.parse import urlsplit
    # Do not claim to parse a shell: allow only a nonexpanding, single command.
    if not cmd or any(c in cmd for c in ';|&<>$`\\\n\r*?[]{}'):
        return 'policy'
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return 'policy'
    if not argv:
        return 'policy'
    if argv[0] in {'ls', 'pwd', 'whoami'}:
        return None
    # A no-query GET remains network-gated. No redirects, uploads, extra requests,
    # output paths, options files, proxies, environment changes or arbitrary flags.
    # -q must be first: otherwise curl can load an ambient curlrc before it.
    if argv[:2] == ['curl', '-q']:
        urls = [x for x in argv[2:] if not x.startswith('-')]
        opts = [x for x in argv[2:] if x.startswith('-')]
        if len(urls) == 1 and all(x in {'-s', '-f', '-sf', '--silent', '--fail'} for x in opts):
            u = urlsplit(urls[0])
            if u.scheme in {'http', 'https'} and u.hostname and not (u.username or u.password or u.query or u.fragment):
                return None
    if (_step() >= 3 and _owner_cfg().get('allow_imessage') is True
        and argv[:2] == ['imsg','send']):
        return None  # explicit owner escape hatch; no AppleScript interpreter
    return 'policy'


def classify(tool_name, args):
    """Kit default: unknown tools/commands block, including dispatcher wrappers."""
    if not isinstance(tool_name, str) or not isinstance(args, dict):
        return 'policy'
    if tool_name in KIT_READ_TOOLS:
        return None
    if tool_name == 'terminal':
        return _kit_terminal(args.get('command')) if isinstance(args.get('command'), str) else 'policy'
    if tool_name == 'write_file':
        # The worker may create data, not executable helpers or guard files. The
        # sender validates the actual bytes/schema; this is friendly early feedback.
        target = args.get('path')
        if not isinstance(target, str):
            return 'policy'
        p = Path(target)
        if '..' in p.parts:
            return 'policy'
        if _step() == 0 and p.is_relative_to('/workspace/fixtures') and p.suffix in {'.txt', '.json', '.md'}:
            return None
        if _step() >= 2 and p.parent == Path('/outbox') and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}\.json', p.name):
            return None
    return 'policy'


def check(tool_name, args):
    kind = classify(tool_name, args)
    return _fmt(kind) if kind else None


def pre_tool_call(tool_name=None, args=None, **_):
    # Hermes may skip a raising hook. A policy failure must return a block, never
    # depend on the host treating a Python exception as a refusal.
    try:
        kind = classify(tool_name or "", args if args is not None else {})
        if kind:
            _log(tool_name, kind, '<kit refusal; arguments omitted>')
            return {"action": "block", "message": _fmt(kind)}
        return None
    except Exception:
        return {"action":"block", "message":"BLOCKED: kit policy unavailable; ask the owner/operator. No tool was approved."}


def register(ctx):
    _log("register", "loaded", VERSION)
    ctx.register_hook("pre_tool_call", pre_tool_call)
