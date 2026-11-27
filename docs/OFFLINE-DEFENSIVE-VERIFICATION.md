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

## Not built or run in A693

The items A693 did not build (model proxy, Hermes Anthropic-provider route, live case driver, native-daemon mode, firewall script, isolation probe, packet tagging, watchdog, `kill.sh` and the dummy-key rehearsal) were built in A694: see `docs/LIVE-MODEL-PHASE1-BUILD.md`.

Still not done by either: no host, key, spend, model calls, pilot, live test or deployment. The runc arm is unverified with a real model; gVisor, Opus and planted-note cases were not run.
