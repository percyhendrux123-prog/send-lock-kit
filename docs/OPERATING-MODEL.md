# Operating the practice company

Fake data only. No setup, account, model, Docker or network is needed
for these commands. Python 3.11+ standard library only. These are plain scripts for
the demo data; they are not wired to a live account.

## The interview

The owner runs the interview, not the worker:

```sh
python3 scripts/interview.py --owner-dir /data/guard
```

`/data/guard` is the existing owner-owned guard directory from the one-door
layout. The owner mounts it writable; the worker mounts that same volume at
`/guard:ro`. No new mount or writable worker path is added.
The script uses the same `owner.atomic` writer as the existing owner commands.
It writes `COMPANY.md`, `APPROVALS.md`, and `PRICES.md` there. It does not change
`axiom-owner.json`, approvals, prices in another service or standing permissions.
The directory must already exist, belong to the caller, and have no group or
other write bits. The interview reuses `install.check_node` to enforce those
checks and reject a directory symlink. It reuses `install.check_root` to reject
symlinked ancestors. Existing page destinations must also pass `check_node`:
no symlinks, directories, hardlinks, other owners or shared write bits. Parent
directories must remain trusted: these are pathname checks, not descriptor-pinned
protection against another process replacing an ancestor. Run one owner setup at
a time; never select a shared worker-writable folder.

For a local fake preview, create a disposable owner directory first and pass
`--answers demo/fixtures/answers.json --owner-dir <that-directory>`. The answers
file is a JSON object keyed by the text question numbers `"1"` through `"15"`.
Values are text; put each price on a separate line (`\n` in JSON), for example
`Intro visit: USD 25`. Ordinary written prices such as `Intro visit $25` also
work. One-line lists may use semicolons or a comma followed by a space and a
capital letter: `Basic tune $60; Full overhaul $180`. A proposed split is used
only when every part has an item name and a price; otherwise the line remains
one bullet. Numeric commas and all amounts are preserved. The interview records
owner wording; it does not validate a commercial price list or grant a permission.

Exit 0 means the pages were written. Exit 3 means setup stopped at a question:
required answers 1, 2, 3, 7, 13 and 14 cannot be blank or unknown. Limits are whole,
nonnegative days. A price answer needs at least one item and a price. No files
are written before all required answers are present. All three pages are rendered
and UTF-8 encoded in memory before any write, so invalid text (including an
escaped lone surrogate in JSON) leaves existing pages byte-identical. Exit 2 is
a file/input error, not a missing business answer. Destination checks, staging
and recovery copies precede replacement; an ordinary replacement failure rolls
back earlier replacements. **The three-page write is not crash-atomic.** If a
run is killed mid-write or loses power, the folder can contain a mix of old and
new pages. If restoring one page fails, rollback still tries every other page;
the warning names each failed page (or directory sync) and the recovery folder.
Recovery copies remain in `.interview-*` in the owner directory on rollback failure.

After a kill, interruption or storage error, the owner must stop using the pages,
fix the storage problem and inspect **all three** pages, not just the one named
in a warning. Keep the private `.interview-*` folder until recovery is checked.
Prefer rerunning the interview with the same inspected answers to write a
consistent new set. To recover the old set instead, restore any remaining
`before-COMPANY.md`, `before-APPROVALS.md` and `before-PRICES.md` copies to their
matching page names (successfully rolled-back pages are already restored).
An originally absent page has no `before-` copy; remove its partial new page
only after confirming it did not exist before the run. If the prior state is
unclear, use the owner's known-good backup or ask the operator rather than guess.
Check the full set before using it, then remove the leftover private copies.

Failure to remove temporary copies after a successful save prints a warning;
it does not falsely report that the pages failed to save.
Optional blanks are marked as unset; only questions 9, 10 and 15 have the
specified defaults. The 15 questions are unchanged.

The five owner-kept decisions are fixed text. Directly above the approval-page
answers, the page says those five lines always win over anything below, and
that all answers on the page are the owner's notes, not permissions. Approval
answers (questions 9–12) mentioning money movement, prices/offers, first contact
with strangers, deleting data or legal/contract terms are held in the section
`Held for the owner: conflicts with what the owner always keeps`, not published
as rules. The terminal prints one line per held answer, naming the question and
conflict. Detection is **best-effort, conservative keyword matching**: it can
miss wording or hold a safe prohibition that mentions these topics. The explicit
owner-keeps override is the real guarantee; detection never grants permission.
A held question-12 note is also withheld from the Company page, which points to
the held note in Approvals rather than repeating it as contact hours.

`demo/INTERVIEW.txt` records both real fake-answer runs and their exit codes.
The complete run's pages are in `demo/owner/guard/`. These are previews, not a
live owner configuration or a claimed distinct-user containment test.

## A week with nothing at risk

```sh
python3 scripts/practice_week.py --all
python3 scripts/practice_week.py --day 5
```

Day 1 is November 9, 2026, at 08:00 in America/Chicago. Each day uses the real
rundown renderer and fake events from `demo/fixtures/practice_week.json`.
`--day N` rebuilds the earlier days privately and prints day N and the summary
through that day. No state is left behind. Repeated runs have identical output.

The practice configuration remains at step 0. The real sender refuses to send
at step 0, even with approval. To teach the later send rules without changing
that rule, a separate disposable **fake send lesson** has step 3 locally. It
calls the unchanged `owner.approve` and `sender.process` in-process. Its receiver
only stores a fake receipt in memory; it opens no socket and has no send key.
It demonstrates a hold, exact-approved send, changed-draft refusal and missing-
approval hold. The actual plugin hook blocks the worker's direct send attempt;
the owner sees a plain explanation, not the technical hook response. Tests
inspect the real sender decisions and unchanged practice configuration bytes.
This does not prove Docker/network containment,
connect a worker, graduate the owner or change any real step or permission.

The summary distinguishes earlier holds from the later approved send. Two draft
versions are approved in the script; only the unchanged one reaches the fake
receiver. `demo/PRACTICE-WEEK.txt` is the real full replay output.

## Weekly and monthly numbers

```sh
python3 scripts/weekly.py --fixtures demo/fixtures --now 2026-11-16T08:00:00-06:00
python3 scripts/monthly.py --fixtures demo/fixtures --now 2026-11-16T08:00:00-06:00
```

Actual output is saved in `demo/WEEKLY.txt` and `demo/MONTHLY.txt`.
Both scripts accept the same fixture/config/clock choices and
`load(business, now)` adapter contract as the rundown. They also accept
`--live --businesses <owner-config>`, but no real read was exercised. The current
readers do not supply lead, send, billing or historical task collections. Those
figures correctly print `not available`. Adding a source is future work, not an
invitation to guess from inbox subjects or current task status.

The optional `numbers.json` fixture is an object keyed by exact business name.
Each value contains complete histories, not just today's search results:

- `leads` and `replies`: records with offset timestamp `at`. Each row is one new
  lead or one reply that was actually sent, not a draft or approval.
- `invoices`: unique `id`, offset issue timestamp `at`, `amount`, and `currency`.
- `payments`: `invoice_id`, offset receipt timestamp `at`, `amount`, `currency`.
- `renewals`: `client` and `due` (local date or offset timestamp).
- `standing_permissions`: the current owner's exact `kind` / `to` pairs.
- `task_history_complete: true`: asserts the full task history in `tasks.json`.
  Every task needs `created_at`; closed tasks also need `completed_at`. Due dates
  must describe the historical schedule; reschedules need a richer source.

A present empty collection is known empty and may produce 0 or `none`. A missing
collection or required field produces `not available` for the affected number.
The same invoice identity validator is used for invoiced totals, balances and
weekly money. Missing, blank or repeated invoice IDs make those money figures
`not available`, with one plain reason per report. Each payment also needs a
link to a known invoice before it can enter cash-received or balance figures.
An invalid payment link does not hide an independently valid invoiced total.
No invalid ledger is silently deduplicated. Other valid figures remain visible.
Do not supply a partial collection as if it were complete. Wrong currency,
negative/nonfinite amounts, overpayments or payments before an invoice cannot
be reported as a healthy zero balance. No partial report is printed on file or
configuration failure. No account, task, invoice or permission is changed.

Weekly flows cover `[now minus 7 local days, now)` and the preceding seven days.
Overdue money and late tasks are balances at those two endpoints. Local calendar
days determine overdue invoices: age reaches `invoice_overdue_days` from issue.
Tasks are late the day after their due date. Future creations and completions
are respected; a present-day task status alone cannot prove a past count.

The monthly close is month to date. **Earned means invoiced**, not cash received,
profit, tax income or revenue recognition. Owed is all issued invoices minus
payments received by now. Amounts use decimal cents in one configured `currency`
(USD for Northside). Refunds, credits and multiple currencies need another
reader and are not invented. Renewal dates cover today through the next 29
local dates, excluding day 30. The standing-permission list is print-only:
“keep or remove this permission?” is a review prompt, not an interactive change. The fake list does
not enable any permission in the practice configuration. Owner changes stay in
the existing owner setup/review workflow; no new permissions command is introduced
here. `owner.py` is not a standing-permission editor. For operators, its existing
commands remain limited to exact-draft approval and step selection:

```sh
python3 deploy/one_door/owner.py --root /data approve draft.json --sha256 <inspected-sha256>
python3 deploy/one_door/owner.py --root /data step <0-to-4>
```

These commands are documentation, not permission to run them on a live system.
