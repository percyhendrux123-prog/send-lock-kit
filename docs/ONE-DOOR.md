# One-door kit: operator runbook

**Fake data only.** The offline tests touch no real account, credential,
message or service. Read
[THREAT-LIST](THREAT-LIST.md) before interpreting any passing test.

## Offline verification

From the repo root (Python 3.11+ stdlib):

```sh
bash demo/verify.sh direct-shell
```

This runs every existing and new test file, a real fixture rundown, local
loopback fake HTTP tests and shell parsing. It does not start Docker or Hermes.
The old send-lock matrix is explicitly historical (`_legacy_classify`). The
registered/default classifier has its own exact 46+3 regression and
malformed-input/hook/step/guard tests. All 46 blocked payloads are unchanged;
the harmless curl positive control adds only the newly required first `-q`.

## Host Docker acceptance (operator only)

Use a disposable approved Linux Docker 28+ daemon (e.g. Colima) with Compose v2.
Do not start/stop an existing VM or change its settings automatically. Have a
reviewed `python:3.12-slim` image **already local** or pass an already-local
immutable reference as `DOOR_BASE_IMAGE`. The script refuses implicit registry
pulls. No packages are installed at build time; build network is `none`.

```sh
bash deploy/one_door/test_container.sh 2>&1 | tee /tmp/one-door-docker.txt
```

Use `set -o pipefail` in the invoking shell to preserve the harness exit code
when teeing. The script uses a unique project/image, only project-scoped named
volumes, and trap cleanup; it never points at real data folders or other Compose
projects. It runs the five required containment attempts plus guard/config/secret
checks, true proxy transport denials, fake allowlisted routes and host bridge
listener tests. It then proves no-approval hold, exact-approved send, replay
refusal, changed-draft refusal and step-2 approved hold using receiver counts,
exact fake payload and journal events. Success prints:

`One-door Docker integration: PASS (fake services only)`

**That line is the expected output; run the script to observe it.**

The host-test-only `fake-host` profile briefly opens a fake HTTP listener on an
OS-assigned port in the Docker daemon's host network, to detect the plain-internal-
bridge host bypass. It contains no secret or filesystem-serving endpoint and is
removed by cleanup. This is an explicit temporary test service, not a worker
network privilege. Do not run the harness on a shared production daemon. If host
networking or isolated gateway mode is unsupported, stop; do not weaken the test.
`DOOR_WORKER_SUBNET` and `DOOR_WORKER_GATEWAY` can select a noncolliding test subnet.

The Compose file is JSON (valid Compose YAML), explicitly selected with `-f`.
Worker is UID 10001; sender 10002; owner 10003. Sender is deliberately inert by
default (`sleep infinity`); the test invokes one selected draft at a time. Its
standalone long-running entrypoint is `python3 /kit/sender.py`, but that daemon
is not auto-started by installation. Keep repeat processing/manual retries off
until the owner has reviewed audit/queue retention and ambiguous-delivery handling.

## Approval and step setting

Owner CLI runs outside the worker, as the separate owner user with the approval
volume writable. The worker cannot run Docker exec, reach an approval endpoint,
or write that volume. The owner inspects the complete draft **and** recipient,
kind and body, obtains its SHA-256, and supplies that inspected digest:

```sh
python3 /kit/owner.py --root /data step 3
python3 /kit/owner.py --root /data approve first.json --sha256 <inspected-digest>
```

These are command shapes for the owner container, not commands to run against a
real account. Approval creation rechecks the digest against the current draft.
Change step only in `axiom-owner.json`; default/missing/unreadable/invalid is 0.
Never treat the worker's claimed approval as an approval. Step 4 permissions are
exact short `(kind,to)` objects in `standing_permissions`. New kinds/recipients
fall back to per-item approval. `allow_imessage` is false; optional true allows
only a plain `imsg send` at step 3+, not arbitrary AppleScript/code. It explicitly
abandons the one-door promise for that Mac path and is not enabled in this demo.

A draft is a flat JSON object with exactly `kind`, `to`, `body` strings, filename
`[A-Za-z0-9_-]{1,64}.json`, at most 64 KiB; each field at most 20,000 characters.
Drafts must be readable by sender (demo writes mode 0640, shared sender GID).
Approval is exactly `draft_id` (basename without .json), `sha256`, `approved:true`.
IDs are single-use after any attempted delivery. Edited content needs a fresh
approval; after an attempted send it also needs a fresh ID. Audit contains
IDs/hashes/reasons, not credentials or bodies. Receiver receipts are fake test
fixtures only. Do not restore/erase the attempt journal after delivery.

## Allowlist configuration, not an open proxy

`egress.json` maps route names to exact upstream origins, allowed methods and
path prefixes. All shipped origins are `http://fake-api:8080` on the outside fake
network. Worker clients use explicit `MODEL_BASE_URL`, `GOOGLE_READ_BASE_URL` and
`OWNER_INBOX_BASE_URL`; there is no HTTP_PROXY/HTTPS_PROXY promise. The integration
checks those actual environment values. Google and owner inbox are GET-only;
only model requests can POST. CONNECT, absolute proxy URLs, redirects, path
traversal/encoded paths and unknown routes refuse.

The plugin's plain-GET terminal shape is now `curl -q -s <URL>`: `-q` must be
first so ambient curl configuration cannot add methods, bodies or extra URLs.
The old no-`-q` shape refuses. This does not open POST, auth headers, options files
or arbitrary code. Kit refusals tell the worker to explain the refusal to the
owner/operator in the current conversation; the human contacts the operator through
their approved channel. There is no automatic `for_axiom.py`/Mac watcher path in
this policy, and Python is still blocked. The gateway applies an absolute five-second
client read deadline covering request headers and body, and at most 16 concurrent
handlers. Slow-drip bytes do not reset that deadline. Excess connections close
without a handler; capacity returns when a request finishes or times out.
Response writes and upstream I/O have separate four-second socket timeouts.
Route/method/origin decisions, redirect refusal and header stripping are unchanged.
Loopback tests cover stalled headers, withheld bodies, slow drips, saturation and
capacity recovery; they are not Docker or production load-test evidence.

For a real integration, review/configure provider base URLs, separate scoped
read credentials at the gateway, real Google path mapping, TLS/streaming needs,
owner chat identity and native Hermes tool exposure. Do NOT attach worker to
the outside network or mount sender secrets to make an incompatible client work.
The fake Python image is not a logged-in native Hermes image. `config.yaml` is
the reviewed opt-in tool selection template, not proof of native plugin loading.
The design follows the Hermes documentation's whole-agent-in-Docker distinction
and its network-isolation guidance (internal network, dual-homed gateway).
A host-run Hermes terminal backend can instead use documented
`terminal.backend: docker`, `terminal.docker_network: false`, no credential/env
forwarding, but that alone does not isolate host-native tools; it is NOT a
substitute for the whole-worker containment shown here.

## Silent, idempotent staging and one-step undo

The VPS bootstrap remains historical fresh-machine provisioning. It is NOT
the containment installer and MUST NOT be run as an upgrade of a live machine.
One-door staging never runs the official installer, network, account creation,
package manager, service manager, gateway start, or data migration.

Disposable rehearsal (no root, no production ownership claim):

```sh
bash deploy/install_one_door.sh --root /tmp/one-door-stage --rehearsal
# Repeating is unchanged; a content upgrade makes a new dated backup.
bash deploy/restore_one_door.sh --root /tmp/one-door-stage --rehearsal restore --backup <dated-backup.json>
```

On an explicitly approved server the administrator omits `--rehearsal` and uses
a dedicated root-owned staging directory outside all worker and user homes. Root
is the distinct system user owning the plugin, Hermes config and owner config;
worker UID 10001 has no write permission. All ancestors must be nonsymlinked;
root and every trusted descendant (including releases, backups and nested release
directories) must have the expected owner, no group/world write permission and
no symlinks. `current` is the single intentional managed symlink. Files are 0444
in releases. Reuse, no-op and restore revalidate this boundary; rehearsal checks
caller ownership, not production root ownership.
No existing Unix user is adopted or changed. Backup is written durably before
atomic `current` promotion. No-op repeat checks actual file bytes and modes,
not just a version marker. Owner step/allowlist settings survive staged upgrades.

The installer builds a release in a private `.release-*` directory, validates and syncs it,
then publishes the complete fingerprint directory with a single rename. Write or
chmod interruption never publishes partial final paths; an identical retry works
without manual deletion. A hard kill can leave an unused private temporary tree;
it is not `current` and is not a published release. After a successful install
(including a validated unchanged repeat), it removes only identified older
private trees: exact `.release-<8 temporary characters>` name, caller ownership,
mode 0700, older directory modification time and an older installer marker tied
to the folder's name, device and inode. Descendants must pass the existing
no-symlink/owner/write checks; the marker is a private regular 0600 file, not a
symlink or hardlink. Unknown added files also preserve the entire private tree.
Deletion requires the platform's symlink-safe, fd-based
`shutil.rmtree`. No cleanup occurs on a failed install; cleanup errors warn rather
than misreporting a completed promotion as a failed install.

Unmarked trees (including leftovers from older installer versions or a kill in
the brief gap after marker removal), altered markers, new trees and non-0700
folders are **not** automatically removed. The owner must inspect those. Unsafe
staging descendants still cause installation to refuse; cleanup is not a way to
bypass the existing trust checks. Run only one installer under its lock and keep
all parents trusted; this is not protection from concurrent owner/root tampering.
Existing partial final paths
left by earlier installer versions are refused, not automatically deleted/adopted; operator
inspection is required. A reported promotion-directory sync failure restores the
prior visible pointer. Persistent disk failure or power-loss durability still
needs operator inspection; this is not a transactional filesystem guarantee.

The returned JSON names the dated backup. Restoring that backup is **one command**;
it atomically reinstates the prior staging pointer (or removes first install).
Both install/upgrade and restore-to-prior/restore-to-absent are exercised in the
offline disposable test. Existing releases/backups are retained, not deleted.
This is undo for staging, not for delivery or a live container cutover. A live
rollout must bind the staged guards read-only and preserve the old image/volumes,
with a separately approved stop/recreate/restore procedure. This repo never
performs that rollout or disturbs a running deployment.
