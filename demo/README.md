# Northside Studio: fake-data demo

All people, mail, calendar entries and tasks are invented. Contact addresses are
`example.com`; phone numbers use the reserved 555-0100–0199 range. Records span
November 8–10, 2026, fixed to `2026-11-10T08:00:00-06:00` for proof. This demo
has **no connected accounts, model login, bot token or sends**. It is an offline
ops-layer demo, not a chat-front-door or model demo.

From the repository root, stdlib-only local verification:

```sh
bash demo/verify.sh space-direct 2>&1 | tee demo/OUTPUT-space.txt
```

When saving evidence in a shell, enable `set -o pipefail` first so a failed suite
cannot be hidden by `tee`. `OUTPUT-space.txt` is real direct (non-Docker) execution,
**not Docker evidence**. Docker acceptance runs on a host.
Do not rename/copy it to `OUTPUT.txt`. (`OUTPUT.txt` is not included in
this copy; it exists only after a real run of `demo/run.sh`.)
The saved `OUTPUT-space.txt` is a direct run of all 25 suites under Python 3.14; the
local checkout path in it was replaced with `<repo>`. `shellcheck` was not installed, so it was not run.

## Real Docker acceptance (operator, on a host)

```sh
bash demo/run.sh
```

Builds `ubuntu:24.04`, runs the actual noninteractive official Hermes install as
a dedicated user, installs/enables send-lock and stages the inert systemd unit.
Build downloads are public Ubuntu/Hermes assets; it never authenticates a model
or platform. The runtime is non-root, `--network none`, no mounts/environment
credentials/capabilities, and runs the fixed rundown plus **every**
`tests/test_*.py` and shell syntax checks. Only a successful real container run
replaces `demo/OUTPUT.txt` atomically. Build/runtime errors preserve any previous
output, which must be checked for freshness before using it as proof.

The wrapper only starts Colima on macOS if Docker is not answering and a
successful `colima list --json` inventory shows no running/unknown VMs. It
accepts an empty inventory or explicitly stopped profiles. Failed/invalid
inventory is refused without start/stop. It stops Colima on exit only after
its own start succeeded. If startup fails, ownership is uncertain: it leaves
cleanup to the operator rather than issuing an unowned stop. No port is published,
no daemon socket is mounted, no volume contains secrets. Image stays local;
remove it manually with `docker image rm axiom-bot-setup-kit-demo` if desired.

Expected slipping examples: worn mats overdue two days, intro confirmation
overdue one day, two-day old intro question, pending delivery estimate and
stalled speaker cable check. Recent/answered mail, closed tasks and cancelled
classes are omitted. The installer tests replace system commands and
the downloaded installer with test-local fakes; they do not assert that a
native VPS install or Docker build has passed.
