# Ubuntu 24.04 bootstrap (operator only)

**Warning:** this bootstrap is historical fresh-machine provisioning,
not the one-door containment installer and never a silent upgrade of a machine already in use.
It uses outgoing-allow networking and worker-owned plugin files. For the new
isolated demo, separate sender, root-owned staging and one-step restore use
[ONE-DOOR.md](ONE-DOOR.md) and [THREAT-LIST.md](THREAT-LIST.md). Do not claim
universal send protection from this bootstrap or the plugin alone.

This installer is for a **fresh**, dedicated VPS, not a machine already in use.
Minimum: **6 GB usable RAM** (6,000,000,000 bytes); allocate at least **6 GiB
and 4 CPUs** to the VM/container for the tested build shape. The author saw a Docker
`demo/run.sh` run fail in a 2 GB VM at the upstream Hermes installer's web UI
build and pass with 6 GB / 4 CPU. That is the author's observation; the log is not
kept in this repo. The bootstrap checks Linux MemTotal and visible
process/ancestor cgroup v1/v2 caps at standard `/sys/fs/cgroup` mount paths before
apt, user creation, firewall or download changes;
below the minimum (or unreadable RAM) it stops with a plain explanation. Swap is
not counted. This is a capacity floor, not a guarantee for future upstream builds;
leave extra headroom and verify the effective limits on the actual Docker host.

The owner approves provisioning; the operator runs it. It creates a password-locked normal
user (default `hermes`), refusing **any pre-existing account** before changes so
an administrator/unlocked/shared account cannot be silently adopted. It installs
system prerequisites, permits the **explicit
actual SSH port**, sets incoming-deny/outgoing-allow ufw defaults, and enables
ufw. It never resets existing firewall rules, opens a web port, disables SSH
password auth, adds SSH keys or grants the bot sudo. Keep an administrator
console and a second SSH session open until access is verified. A provider
firewall is separate; preserve its SSH rule too. If other workloads exist, stop
and write a firewall plan instead of running this fresh-VPS installer.

Preview (no changes):

```sh
bash deploy/install_vps.sh --plan
```

On the approved new VPS, from the reviewed kit checkout, administrator runs
(replace 22 with the actual SSH port; do not guess):

```sh
sudo bash deploy/install_vps.sh --non-interactive --user hermes --ssh-port 22
```

The service user runs the **official** HTTPS installer downloaded from
https://hermes-agent.nousresearch.com/install.sh with `--non-interactive`,
`--skip-browser`, `--skip-computer-use`. No prompt, model login or gateway setup
is invoked. Provider secrets from root's environment are not inherited.
The official installer pulls public Hermes/dependency assets; the script is not
vendored or pinned here, so review upstream changes before production use.
The actual installer was not run in the offline tests; mocked command tests
are not a native Ubuntu deployment result.

The kit installs send-lock in `~/.hermes/plugins/send-lock`, its
relay/self-send helpers and the new rundown/ClickUp scripts in
`~/.hermes/scripts`, enables the plugin through Hermes CLI, and stages
`~/.config/systemd/user/hermes-gateway.service`. On a VPS it enables user
lingering and reloads the user manager. It **does not enable/start the gateway**.
Docker `--container` skips ufw, lingering and every systemctl call (there is no
systemd in the demo); all other install steps still run. Do not use container
mode as a shortcut on a VPS. Existing staged guards/units are refused rather
than overwritten; updates require a separate reviewed plan.

## Owner activation, separately approved

1. Switch to the dedicated user via the administrator. Ensure
   `~/.local/bin` is on PATH. Run `hermes doctor`,
   `hermes plugins doctor ~/.hermes/plugins/send-lock` and
   `hermes plugins validate ~/.hermes/plugins/send-lock` on that installed
   Hermes version. Resolve failures before activation. The manifest declares
   Linux. The wall test loaded the plugin in a real Hermes container, but that is
   not a VPS install, and the offline policy suite alone does not prove native hook loading.
2. Owner performs model login (`hermes model`) and chat setup
   (`hermes gateway setup`) directly on their device/server, and explicitly
   declines any offer to start the gateway until step 5. Credentials never go
   in chat, this repo, Docker build args, shell history or evidence files.
3. Owner re-consents Google with readonly Gmail/Calendar scopes, supplies their
   own ClickUp token, and defines exact business calendar IDs, waiting-label
   **IDs**, list IDs and thresholds in a private `businesses.json`. This kit's
   live reader consumes a short-lived `GOOGLE_ACCESS_TOKEN` from the environment;
   it does not create/refresh grants or automate token storage. Mail waiting is
   based on explicitly curated waiting labels, not an inference from all mail.
4. Set the owner step in `axiom-owner.json` with the operator (missing or invalid
   config means step 0, the most closed setting). The plugin is a short default-deny
   allowlist, not universal every-outbound authorization. ClickUp uses its own fresh interactive CLI
   confirmation and refuses unattended writes. Do not advertise all-surface
   approval enforcement or expose these scripts to autonomous write tools until
   native approval integration has been reviewed. No bot/chat-front-door demo
   or model-powered workflow is claimed by the offline demo.
5. Prove synthetic denied sends, dry-run requests and owner allowlists through
   native Hermes **without any real send**. Only after the owner approves:
   create `~/.hermes/AXIOM-GATEWAY-APPROVED` as the service user, then run
   `systemctl --user enable --now hermes-gateway.service` from that user's
   session. Check `systemctl --user status hermes-gateway.service` and
   `journalctl --user -u hermes-gateway.service`. Do not run a second service via
   `hermes gateway install`; this kit already staged the named unit.

## Undo

Stop/disable only `hermes-gateway.service` as the service user, remove its
approval marker, remove that exact staged unit, and daemon-reload. Keep
send-lock enabled while any agent can run. Revoke new provider/tool grants in
the owners' account UIs. Restore prior firewall policy from the operator's
pre-change record via console; do not blindly disable/reset ufw. Disable
lingering for the dedicated user only if no other user service needs it.
Archive reviewed non-secret configuration, then remove only this dedicated
user/home if owner-approved; never delete a shared user's home. Removing the
plugin alone is not a safe rollback for a still-running agent.

References: official [installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation/),
[gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/),
[plugin documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/).
