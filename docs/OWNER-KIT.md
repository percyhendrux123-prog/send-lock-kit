# Owner kit (the rest of the repository)

The rest of the repo is the owner-agent kit these pieces came from: a rundown
and "what is slipping" report, a ClickUp helper, an interview and a weekly and
monthly numbers routine, an Ubuntu bootstrap and a migration guide. All of it
runs on fake demo data (a made-up studio, "Northside Studio"). The sections
below describe those parts. They are not part of the wall test.

Some older owner-run helpers in `scripts/` are not exercised by the offline
tests and were not run against real services for this copy: `selfsend.py`
(sends a draft to the owner's own addresses through Gmail),
`gmail_auth_readonly.py`, `work_gmail_watch.py` and `alert_health.py`.
`for_axiom.py` (the operator relay: writes a request file for an operator-side
watcher) and `stall_watch.py` (that watcher) are tested with a fake `ssh`.
The names `axiom-owner.json`, `AXIOM_OWNER_CONFIG`, `for_axiom.py` and
`FOR-AXIOM` are kept because the code and tests use them.

## Offline quick start for the demo data (Python 3.11+, standard library only)

```sh
python3 scripts/rundown.py --fixtures demo/fixtures --now 2026-11-10T08:00:00-06:00
bash demo/verify.sh direct-shell
```

Every `tests/test_*.py` also runs independently with `python3`. No pip/npm
install, OAuth login, model or token is needed. The verification script runs
all suites and shell syntax checks; shellcheck is used only if installed.
[Demo instructions](../demo/README.md) distinguish direct-run evidence
`demo/OUTPUT-space.txt` from host Docker evidence (`demo/OUTPUT.txt`, written
only by `bash demo/run.sh`; not included in this copy). Never substitute one
for the other.

## Operating routine (practice week, weekly numbers, monthly close)

[Operating guide](OPERATING-MODEL.md): the owner interview, a fake practice
week, five weekly numbers and the monthly close. These are plain scripts. No
model or account is needed. The real send guards are unchanged.

```sh
python3 scripts/practice_week.py --all
python3 scripts/weekly.py --fixtures demo/fixtures --now 2026-11-16T08:00:00-06:00
python3 scripts/monthly.py --fixtures demo/fixtures --now 2026-11-16T08:00:00-06:00
```

The guide explains the owner-only page path and missing-data rules. Read it
before using the interview or interpreting any number as a business result.
Actual fake-company outputs are in `demo/PRACTICE-WEEK.txt`, `demo/WEEKLY.txt`,
`demo/MONTHLY.txt` and `demo/INTERVIEW.txt`.

## Rundown and adapter contract

`Adapter.load(business, now) -> Snapshot(events, tasks, mail)` is the common
interface for `FixtureAdapter` and `ServiceAdapter` (Google Calendar/Gmail and
ClickUp readers). Fixture mode never constructs the service adapter, reads a
credential or opens a socket. Output is buffered across businesses: a failed
source exits 2 with no partial “healthy” report. Empty sections are omitted.
All-day events have exclusive end dates; today's timed/overlapping events use
the business timezone. Open tasks due today/earlier appear in due/overdue;
closed/done/completed tasks are ignored. No task or mail is modified.

The fixture directory contains four required JSON arrays:

- `businesses.json`: one config object per business, for example:

```json
[
  {
    "name": "Northside Studio",
    "timezone": "America/Chicago",
    "calendar_ids": ["northside-classes@example.com"],
    "clickup_list_ids": ["northside-ops"],
    "mail_labels": ["Waiting_Northside"],
    "slip_thresholds": {"mail_reply_days": 2, "stalled_task_days": 2, "overdue_days": 1}
  }
]
```

- `calendar.json`: `id`, `calendar_id`, `summary`, `start`, `end`, optional
  `status: "cancelled"`. Dates are `YYYY-MM-DD`; timed values require offsets.
- `tasks.json`: `id`, `list_id`, `name`, string `status`, nullable `due` (date or
  offset timestamp), offset `updated_at`. Additional metadata is ignored.
- `mail.json`: `id`, `labels` array, `subject`, offset `last_message_at`, boolean
  `waiting_on_reply`, `waiting_on` (`owner` or `customer`). Fixtures represent
  current thread state, not an unprocessed stream of individual messages.

Names must be unique. Selectors are exact strings; empty selector lists disable
that source for that business. Timezone defaults to UTC. Thresholds are required
nonnegative integer **days**: mail/stall age uses completed 24-hour periods and
qualifies when age **reaches** the threshold. Overdue uses local calendar-day
age, must be at least one day late, and qualifies at the configured threshold.
Tasks still appear in due/overdue below the slipping threshold. The slipping
list names both overdue and stalled reasons when one task has both. A waiting
label is an explicit curation decision, not an AI assertion that mail needs reply.

## Owner-operated live readers (not exercised against real services here)

```sh
python3 scripts/rundown.py --live --businesses /owner/private/businesses.json
```

The owner must separately consent to readonly Gmail + Calendar scopes and
supply short-lived `GOOGLE_ACCESS_TOKEN` and `CLICKUP_TOKEN` through their local
secure environment. No token value goes in an argument, repo or evidence file.
The rundown does not run OAuth, read stored grants, create accounts or refresh
Google tokens. (The separate owner-run helpers `selfsend.py --auth` and
`gmail_auth_readonly.py` do open a Google sign-in when the owner runs them.) There is no scheduled delivery/send step. Use exact label **IDs**
for live Gmail. Multiple waiting labels are ORed and threads deduplicated;
latest non-draft/non-spam/non-trash message metadata decides who waits
(`SENT` -> customer, inbound -> owner). Removing waiting labels resolves a
thread. Calendar expands recurring events for the local day; all readers
paginate, use GETs, finite timeouts and refuse redirects. Validate account,
consent and selectors with the owner before any real read.

## ClickUp helper (v2)

Zero-network previews work without a token:

```sh
python3 scripts/clickup.py --dry-run lists 123 --kind folder
python3 scripts/clickup.py --dry-run tasks 456
python3 scripts/clickup.py --dry-run task abc123
python3 scripts/clickup.py --dry-run create 456 --data '{"name":"Check studio mats"}'
python3 scripts/clickup.py --dry-run update abc123 --data '{"status":"in progress"}'
```

For real use, owner supplies `CLICKUP_TOKEN` locally. Omit `--dry-run` for GETs;
list-task reads paginate. `lists --kind space` reads folderless lists;
`--kind folder` reads lists in that folder. **Every real CLI create/update
prints a redacted frozen request and waits for the owner to type `APPROVE` on
an interactive terminal.** No unattended flag exists. Library writes default
to denial unless the trusted caller supplies an approval callback. This is an
owner-operated CLI confirmation, not Hermes-wide unbypassable authorization;
do not expose the library as an autonomous write tool. No automatic retries:
a failed/uncertain write requires checking the task before trying again.
HTTP response bodies, exception strings and authorization values are not logged;
echoes of the supplied token are redacted from returned/printed data too.

## Provision, migrate, undo (VPS bootstrap, historical)

- [Ubuntu install and owner activation](INSTALL-VPS.md)
- [VPS -> Mac mini migration, never-copy list and undo](MIGRATE-VPS-TO-MAC-MINI.md)

The installer stages an **inert** systemd user service. It never runs model
login, gateway setup or gateway start. The send-lock manifest declares macOS and
Linux. `hermes plugins validate plugins/send-lock` passes, and the wall test
loaded the plugin in a real Hermes container, but a full VPS install has not
been run. The plugin is not a promise of universal outbound enforcement. Keep
the gate and closed doors while configuring any real integration.
