# Move a bot from VPS to Mac mini (owner + operator)

A reviewed **selective migration**, not a copy of `~/.hermes`. Never run the old
and new gateways together. The owner approves the cutover; no real customer data belongs
in this repo, build logs or review prompts.

## 1. Inventory and freeze (VPS)

Record the installed Hermes version, kit commit, service user, business
selectors, enabled plugins, scheduled jobs and the exact gateway unit name.
Record pending approvals/drafts and reconcile delivery outcomes with the owner;
do not replay a queue just because a server changed. Pause approved schedules,
then stop/disable this kit's `hermes-gateway.service` using the service user's
`systemctl --user`. Remove the activation marker so an accidental restart
cannot reconnect. Verify the process is stopped from the administrator console.
Keep the VPS and its encrypted disk available but stopped during acceptance.

## 2. Copy only a reviewed allowlist

Use the kit's reviewed git checkout/bundle (no remote required), the exact
send-lock source/manifest and approved helper scripts, custom skills/rules
that have been manually scrubbed, and a newly written **non-secret** business
config containing names, calendar/list/label IDs, timezones and thresholds.
Recreate owner allowlists locally after confirming identities. Transfer only
explicitly approved non-secret documents over an encrypted owner-controlled
channel. Preserve a local backup of the Mac's existing config before any merge;
never replace its entire Hermes home or another profile.

**Never copy:** `.env` (including hidden variants), `auth.json`, Google token
JSON, OAuth refresh/access tokens, bot tokens, `CLICKUP_TOKEN`, API keys,
`credentials.json`, OAuth client secrets, cookies, browser profiles, SSH private
keys, keychains, `.netrc`, `.gnupg`, credential caches or a full Hermes backup.
Do not copy state/session databases, logs, transcripts, memory, pending delivery
queues, old cron files or provider tool stores wholesale: they can contain
secrets, client information or jobs that send twice. A config YAML can also
contain inline secrets; **rebuild an allowlisted subset**, do not assume it is
safe by extension. Do not use a full `hermes backup`/import to bypass this rule.
Copy no Linux venv, executable, cache or systemd directory to macOS.

## 3. Fresh install and re-consent (Mac)

1. On a supported Apple Silicon Mac mini, the owner uses the official installer
   from https://hermes-agent.nousresearch.com/install.sh (review the current
   docs first). Do not run Ubuntu/ufw/useradd steps on macOS.
2. As the intended Mac user/profile, install the reviewed send-lock under its
   own `~/.hermes/plugins`, enable it and run native plugins doctor/validate.
   Keep every outbound door closed during setup. Recreate safe rules and
   helpers from the allowlist, resolving Linux-only paths.
3. Owner signs into the model provider afresh with `hermes model`. Owner grants
   Google readonly Gmail/Calendar scopes to the right account and grants a
   new least-privilege ClickUp credential locally; no credential is transmitted
   to the operator. The rundown's supplied Google bearer token is short-lived and has
   no auto-refresh implementation. Confirm account and business selectors
   privately before reading real data.
4. Re-consent the chat integration in `hermes gateway setup`; rotate/reissue bot
   tokens in the platform UI where supported. Verify allowlisted owner IDs,
   scopes and voice/transcription permissions with the owner, not from old logs.
   Decline any automatic start offer while the VPS stop is unverified.
5. macOS uses **launchd, not systemd**. The owner invokes
   `hermes gateway install` for the intended user/profile, inspects the generated
   LaunchAgent and uses `hermes gateway status` to identify it. Do not copy the
   VPS unit, enable Linux lingering or invent a hardcoded launchd label. Hermes
   versions may start the service during installation; do this only after the
   VPS stop and owner cutover approval are verified. Keep sends disabled.

## 4. Accept, then retire

First run the deterministic fake fixture rundown, every offline test and native
plugin diagnostics. Verify denied sends and ClickUp dry-run without a real
send. Verify a single gateway, owner-only chat allowlists, local sleep/wake and
restart behavior. Recreate only reviewed schedules, explicitly one by one with
new IDs and owner approval. Reconcile any uncertain delivery before a retry.
An offline test result is not a proof of connected chat/model behavior.
Only after the owner accepts the Mac should they revoke old VPS grants/sessions
(in each provider/Google/ClickUp/chat UI), remove old secrets securely through
the provider's disk-destruction process and terminate the paid VPS with the owner's
approval. No spend/billing step is automatic.

## Undo (before retiring the VPS)

Stop the Mac gateway with `hermes gateway stop`, verify the actual launchd job
is no longer running, disable/unload only the generated job documented by
`hermes gateway status`, and keep send-lock enabled. Revoke the Mac's new
integration grants. Do not delete an existing Mac user's home/profile.
If the owner chooses to return to VPS, re-consent there when old grants were
revoked; reconcile pending deliveries first. Recreate the VPS activation marker
only after explicit owner approval, then start the single reviewed systemd user
unit. Never restart both sides. If the VPS disk is already destroyed, rollback
is a fresh install/re-consent, not recovery of old tokens.

References: [installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation/)
and [gateway service management](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/#service-management).
