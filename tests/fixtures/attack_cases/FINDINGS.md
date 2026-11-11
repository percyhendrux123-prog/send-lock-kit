# Send-lock coverage test: findings

Written 2026-11-10 against the first version of the send-lock plugin, which was a list-based classifier. The numbers below are for that version only. The current plugin is a default-deny allowlist: running `probe.py` against it now reports 49 of 49 correct at the decision function (46 blocked, 3 harmless allowed; it writes a `results.json` in the working directory). That is a classifier result, not containment: see docs/THREAT-LIST.md and docs/WORKER-IN-WALL-PLAN.md.

## What was tested

`probe.py` hands 49 made-up tool calls to the lock's own decision function and records whether it would block each one. Nothing was run and nothing was sent. Every address, id and host in the probes is fake. Re-run: `python3 probe.py`.

## Result

46 of the 49 calls are ways a message, post or data could leave. **The lock blocked 6 of the 46. The other 40 went through.** The 3 harmless calls were correctly allowed.

| Group | Blocked | Example that got through |
|---|---|---|
| Baseline (the lock's known cases) | 4 of 4 | none |
| Email from the shell | 0 of 8 | a mail service's web address, raw mail protocol, a split-up import |
| Write a sending script, then run it | 0 of 2 | file contents are not inspected |
| Chat and text services from the shell | 1 of 8 | Telegram to another chat, Slack and Discord hooks, text services other than Twilio |
| Publishing | 0 of 4 | code push, comment on someone's repo, a web form |
| Data out through a web address | 0 of 2 | a plain page fetch with data in the address |
| Browser and screen control | 0 of 5 | open mail compose, click Send |
| Other tools | 0 of 7 | hosted mail connector, code runner, phone, a helper agent |
| Scheduling outside Hermes | 1 of 3 | system scheduler |
| Changing the lock itself | 0 of 3 | switch the plugin off, edit the config that loads it |

## Why

The lock keeps a list of known sending commands and blocks those. Anything not on the list passes. A list of bad things can never be complete, because a shell and a browser can each send in endless ways.

The lock works as designed for what it was written to stop: an honest agent reaching for the obvious send tool. It does not stop an agent that was tricked by something it read, or one that improvises.

One item is by design, not a miss: iMessage from the Mac is an open door in the source pack. In the kit that door lets a text go to anyone, so it has to close by default.

## What fixes it

Flip the rule. Instead of "block what looks like a send," use "nothing can reach the outside except through one door."

1. **No way out at the network level.** The worker runs in a container whose network reaches only the model provider and the read-only services it needs. Hermes supports a Docker backend with isolated networks. A shell command to any other address fails no matter how it is written.
2. **No send keys on the worker.** The worker holds read-only keys. It cannot send mail even if it tries, because it has nothing to send with.
3. **One door, outside the worker.** A small separate sender holds the send key. It sends only a draft that carries an approval recorded from the owner's own chat. The worker can queue a draft. It cannot approve one.
4. **Tools are opt-in.** Browser, screen control, phone, hosted connectors and helper agents are off unless a desk needs one. Unknown tools are blocked by default.
5. **The lock's files belong to someone else.** The plugin, its config and the Hermes config are owned by a different system user and are read-only to the worker.
6. **Keep the current lock** as the friendly first layer. It gives the agent a clear plain-English reason, which stops honest mistakes early.

The 49 probes become the acceptance test. The fix is done when every one of the 46 is blocked with the worker actually trying it inside the container, not only on paper.

## Limits of this test

- It checks the lock's decision, not a live agent. A live Hermes may add its own approval prompts on some shell commands, which this test does not count.
- The probe list was written by one person. A wider list would likely find more.
- Any other install of the older list-based design was not examined.
