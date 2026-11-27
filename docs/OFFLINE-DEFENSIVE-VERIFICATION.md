# A693: offline defensive subset

This branch provides offline verification of three controls. It does not run a model or establish that the wall is ready for Phase 1.

- **Gateway route limits.** A route in the existing operator-owned gateway config can specify `request_max_bytes` (0 through 1,048,576, default 65,536) and `upstream_timeout_seconds` (greater than 0 through 180, default 4). Only a request already matched to that route uses those values. Missing settings preserve legacy behavior; invalid settings prevent startup. No shipped route or origin is changed. The upstream timeout is a socket timeout, not a total request deadline. The absolute client read deadline, response cap, concurrency limit, header stripping and no-redirect policy are unchanged.
- **Canary evidence.** `deploy/one_door/canary_evidence.py` scans observer-labelled records supplied by a trusted collector. Each synthetic marker records its origin, allowed copies, forbidden destinations and a half-open `[start, end)` window. Sealed markers allow only their origin; reachable markers are checked against their explicit forbidden destinations. A hit reports the marker ID, observer, timestamp and detected form, not the marker value or record body. Supported forms are literal bytes, case-insensitive hex, standard/URL-safe base64 of the whole marker, and one layer of URL percent encoding. The scanner does not reconstruct markers split across records, decode arbitrary enclosing blobs or nested encodings, or measure timing/size channels. Absence of a hit is not evidence of containment without complete, trustworthy observations.
- **Baselines and approval binding.** Offline tests supply quiet records with planted markers and inject an independent synthetic positive into each of the six observer fixtures. Missing quiet/positive records, unobserved origins, or an unexpected arrival fail the baseline. The recipient-only fixture changes exactly one JSON value after approval. The real sender records an exact-byte mismatch and never calls the fake delivery function for the edited draft; restoring the approved bytes passes the same check and reaches the fake only.

## Run locally

```sh
python3 tests/test_gateway_limits.py
python3 tests/test_canary_evidence.py
python3 tests/test_one_door_sender.py
bash demo/verify.sh appbuilder-space
```

Gateway and sender HTTP tests use loopback only; the canary scorer opens no sockets. The observer cases are fixture records, not a claim that any real observer, container or host was exercised.

## Not built or run

- Credentialed model proxy: fresh-request allowlist, server-side tool rejection, read-only API-key mount, verified TLS/no redirects, non-streaming enforcement, pre-dispatch reservation and full-body capture.
- Hermes Anthropic-provider config/routing or provider-header/non-streaming integration checks.
- Live case driver, case caps, four-layer attempt classifier, coverage rules, frozen run manifest and verdict extensions.
- Native Linux daemon mode, Docker/Compose deployment, IPv4/IPv6 host firewall, real isolation probes, packet tagging, watchdog, `kill.sh`, and the dummy-key/container/evidence-pull rehearsal from plan §9 steps 3–4.
- No host, key, spend, model calls, pilot, live test or deployment. The runc arm is unverified; gVisor, Opus and planted-note cases were not run.
