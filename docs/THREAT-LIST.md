# Threat list: one door out

This is a fake-service containment demo, not a production deployment.
The plugin alone cannot promise that nothing leaves. Model requests necessarily
carry data to a permitted service. Read-only requests can carry data in URLs.
An owner's ordinary chat replies may also leave without using model tool hooks.

## What the layers do

| Layer | What it stops | What it does not stop |
|---|---|---|
| Friendly plugin | Unknown tools, dispatcher wrappers, browser/screen/phone/connectors/delegation, arbitrary shell/code, known send commands, plugin-disable/remove commands, config edits. Default is a short allowlist, not a list of send spellings. | Code that never passes a model-tool hook, a missing/disabled plugin, malicious host code, permitted requests carrying data. It is not the network firewall. |
| Docker network wall | Worker has only one internal network. Only the fixed-route gateway is dual-homed. Docker isolated gateway mode leaves no host bridge address on the worker network, closing the host-listener bypass of a plain internal bridge. No worker host network, Docker socket or privileges. | A Docker/kernel escape, root/daemon operator, a compromised gateway, data sent to permitted services. Actual engine enforcement remains a host test, not a static-file claim. |
| Fixed-route allowlist gateway | Only named owner-configured origins, methods and path prefixes. Google/owner-inbox routes are GET only. Model route allows the required POST. Unknown origins/routes, forward-proxy requests, CONNECT and redirects refuse. | It cannot judge a model prompt's content, guarantee that a GET upstream has no side effects, or turn a broad credential into read-only access. Use actual read-only OAuth scopes and narrow paths. |
| Separate sender | Only sender mounts the send-credential file. Worker writes outbox drafts; owner writes approvals on a different UID/volume. Worker mounts approvals/config/plugin read-only. No approval-writing network endpoint exists. | An operator, root, a stolen owner account, or a compromised sender can bypass it. The fake token is deliberately public test data, not evidence of credential secrecy. |
| Approval/hash | Exact raw draft bytes include recipient, kind and body. Owner supplies the inspected hash. Sender loads once and hashes that snapshot. An edit invalidates approval; unknown fields, duplicate JSON keys, oversized/nested input, symlinks/FIFOs/hardlinks refuse. | It cannot know the human actually understood the preview. No chat identity/approval adapter is implemented. Owner CLI is an administrative path, not a worker tool. |
| Replay journal/audit | Exclusive process lock; durable `attempt` before I/O; file AND parent-directory fsync. Every normal hold/refusal/send is appended. An attempted ID is never retried, even after a disconnect. Log failure prevents delivery. | It is at-most-one-attempt, NOT exactly-once delivery. Crash between receipt and success log is uncertain and stays consumed. A corrupt/unavailable disk can prevent refusal logging; it still prevents sends. Owner must reconcile manually and use a new ID. No automatic retry. |
| Owner step config | Plugin and sender re-read step 0–4. Invalid, missing or unreadable config means 0. Steps cannot open direct send tools. Step 4 standing permission is a short exact list of `(kind, recipient)` pairs, not all recipients of a kind. | A root/owner change during an in-flight authorized attempt cannot recall that request. Changes apply on the next decision. Owner revocation is not a transactional distributed cancellation system. |
| File ownership | Installer stages guards under root (not worker UID 10001). Demo owner UID 10003 and sender UID 10002 differ from worker. Bind/volume read-only mounts, not regexes, stop bypass writes. | Running the historical VPS bootstrap alone is not containment. Worker-owned parent directories, extra mounts, additional hooks/plugins or user services can break a real install. Rehearsal UID/modes are not proof of root ownership. |

## Step meanings

- **0 practice:** fixture data files under `/workspace/fixtures` only; sender disabled.
  Harmless read/list tools remain available. No outbox drafts accepted by plugin.
- **1 watch:** no drafts accepted; sender refuses bypassed outbox writes.
- **2 draft:** plugin accepts outbox JSON drafts; sender holds even an approved draft.
- **3 send with OK:** exact per-draft hash approval required.
- **4 standing permission:** exact owner-listed kind/recipient pairs can skip per-item
  approval. Everything else still needs step-3 approval. Default list is empty.

`allow_imessage` is false. Setting it true at step 3+ is an explicit owner escape
hatch for a plain `imsg send` Mac command (not AppleScript), not part of the one-door guarantee. The Linux
demo does not expose iMessage. Never turn it on as a workaround for a held draft.
Unknown tools remain blocked; opt-ins require owner/operator code/config review.

## Probe evidence and limits

The 46 should-block payloads are preserved verbatim. The harmless curl positive control carries a first-argument
`-q`, only to the harmless curl positive control, so it cannot load ambient curl
configuration. The original no-`-q` shape now explicitly blocks in a separate test.
The other two harmless controls are unchanged; no negative probe is relaxed.
All **46 should-block cases block at the plugin's decision function**, with **3 harmless cases allowed**.
**This is a classifier-level result. Inside a real Hermes runtime the picture is different:** in the worker-in-wall test (`docs/WORKER-IN-WALL-PLAN.md`), 14 of the 46 cases passed the plugin and were stopped only by the container wall. Do not read the classifier result as containment.
There are no probe exemptions labeled “network layer only.” These are classifier
calls, NOT live browser/connector/phone sends. Unknown/wrapper denial, registered
hook malformed-input behavior and additional shell/config probes are separate tests.
The inherited pack matrix now explicitly exercises `_legacy_classify`; its old
allow expectations are historical, never used as the kit policy or a fallback.

The host script additionally bypasses the plugin intentionally with real Python
inside the worker. It checks outside POST/GET/raw TCP 25, approvals/plugin/config
writes, credential absence, actual forward-proxy GET/POST/CONNECT refusal, allowed
fake routes, a live host-listener bypass control, and the four sender scenarios.
Positive controls first prove the fake outside HTTP/SMTP and host listener exist.
Network-denial assertions reject HTTP responses; proxy-denial assertions require
HTTP 403, not connection failure. Offline unit tests are not container evidence.

## Known limits before any real rollout

1. Docker 28+ with `gateway_mode_ipv4: isolated` is required; unsupported engines
   must fail, not quietly fall back to a plain internal bridge. The host test needs
   host networking for a disposable fake positive-control listener; on Docker
   Desktop that may be unavailable. Do not enable it automatically. Colima/Linux
   daemon support and the configured subnet must be checked by the operator.
2. Docker's embedded DNS is still a special path. Worker upstream DNS is set to
   its own loopback (no resolver); internal service discovery should still work.
   A reserved-name failure is not packet-capture proof that queries never left.
   Before live use verify DNS forwarding/IPv6/host routes on the actual daemon.
3. Gateway is a bounded JSON reverse gateway, NOT an HTTPS CONNECT proxy. Clients
   must use the named base URLs. HTTPS upstream certificates are verified; no
   streaming, OAuth refresh adapter, TTS adapter, Google API path translation,
   production provider credentials, or native Hermes model login is exercised.
   The supplied Python worker proves the boundary around arbitrary executed code,
   not that a native Hermes session has been integrated and exercised.
4. In the demo both networks are internal and every origin is `fake-api`. A real
   externally routed gateway, provider/Google/chat allowlist, scoped credentials,
   owner identity checks and native Hermes runtime require a separately reviewed
   owner-approved integration. Sender endpoint is fixed by operator, never by draft.
5. Never forward the real send credential into the worker, skill mounts, a model
   request, tool gateway or general connector. No hosted remote browser, external
   MCP/connector, delegation, phone or screen tool is part of this boundary.
6. Host root, Docker socket access, new bridge attachments, additional DNS routes,
   broad network tunnels or mutable guard ownership invalidate the guarantee.
   Read-only Google/model/owner hosts are still possible data-leak destinations.
7. Queue flooding, repeated hold logs and disk exhaustion are availability risks.
   The demo has bounded files and worker tmpfs/PID limits, but no production queue
   quota, retention policy, monitoring or daemon sandbox escape testing.
   **Transport hardening:** client headers and bodies share an absolute
   five-second read deadline (not reset by slow-drip bytes), and only 16 handlers
   run concurrently. Excess connections close before a handler is created;
   completion, disconnection and timeout release capacity. Responses use a
   separate four-second write timeout. Loopback tests cover idle/partial headers,
   withheld bodies, header/body drips, saturation and subsequent allowed/denied
   requests. This narrows the slow-client risk; it does not prove fair scheduling, rate limits
   or production availability. An attacker can still occupy all 16 slots until
   their deadlines; upstream socket timeouts are inactivity limits, not a total
   upstream-response deadline. Docker PID-budget/load tests remain host work.
8. Install/restore are offline staging transactions, not automatic live cutovers.
   No service, account, real data or live config is modified here. Stop/recreate
   an approved runtime to bind a changed release; existing mounts/processes do not
   magically change on a staging symlink update. Retain both images and volumes.
   Restore does NOT erase audit history, undo a sent message or roll back owner
   approvals. Do not roll back the attempt journal after any actual delivery.

**Do not deploy for real use until the operator has run a native code review, the real Docker
harness, ownership/native config checks and the specific rollout/rollback rehearsal.**
