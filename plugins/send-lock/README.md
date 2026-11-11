# send-lock

A default-deny `pre_tool_call` hook for Hermes. It lets a short explicit list of
tool calls through and blocks everything else, with a plain-English message that
tells the agent why.

It is a friendly first layer, not a containment boundary. See "Limits" below.

## What it allows

Everything not listed here is blocked, including unknown tools and wrappers.

| Call | Allowed when |
|---|---|
| `read_file`, `search_files`, `skill_view`, `skills_list`, `todo`, `todo_list`, `text_to_speech` | always |
| `terminal`: `ls`, `pwd`, `whoami` | always (a single command; no pipes, redirects, `$`, backticks, globs or newlines) |
| `terminal`: `curl -q -s <URL>` (also `-f`, `--silent`, `--fail`) | one plain `http(s)` URL with no query, fragment or credentials |
| `write_file` of a `.txt`, `.json` or `.md` file under `/workspace/fixtures` | owner step is 0 |
| `write_file` to `/outbox/<id>.json` (`id` is `[a-zA-Z0-9_-]{1,64}`) | owner step is 2 or above |
| `terminal`: `imsg send ...` | owner step is 3 or above and `allow_imessage` is `true` |

A hook error also returns a block, never an allow.

## Owner config

Read at call time from `<HERMES_HOME>/axiom-owner.json`, or from the file named by
the environment variable `AXIOM_OWNER_CONFIG`. (The file and variable names are
kept for compatibility with the rest of the repository.)

| Key | Meaning |
|---|---|
| `step` | integer 0 to 4; anything else, or a missing or unreadable file, means 0 |
| `allow_imessage` | `true` opens the `imsg send` escape hatch at step 3 or above |
| `owner_name`, `axiom_contact` | shown in block messages |

## Disclosure

Verified by reading `__init__.py`:

- **Reads:** the owner config file above (outside the plugin directory, inside the
  Hermes home unless `AXIOM_OWNER_CONFIG` points elsewhere); the environment
  variables `HERMES_HOME` and `AXIOM_OWNER_CONFIG`; the tool name and arguments of
  each call. The legacy matrix (below) can also read `<HERMES_HOME>/config.yaml`,
  but only tests reach that code.
- **Writes:** appends one line per refusal, and one line at load, to
  `<HERMES_HOME>/logs/send-lock.log`. A refusal line holds the tool name and the
  block kind; the arguments are omitted. Nothing else is written.
- **Blocks:** every tool call not in the allowlist above. After install, expect
  most tools (for example `patch`, `memory`, `skill_manage`, browser, web, code
  execution, messaging) and most shell commands to be refused.
- **Network, subprocess and model calls:** none. It does not store or read
  credentials, and it does not update itself.
- **Surface used:** `ctx.register_hook("pre_tool_call", ...)` only. No tools,
  middleware or environment variables are registered or required.

## Limits

- **The plugin alone is not a containment boundary.** In the worker-in-wall test
  (a real Hermes in a container, driven by a scripted fake model, on fake services
  only), 14 of 46 attack cases passed this plugin and were stopped by the container
  wall. 32 were stopped by the plugin. With the plugin off, the wall stopped all 46.
  Evidence: `docs/evidence/worker-in-wall-*/verdict.txt` in the repository root.
- A permitted `curl` GET can still carry data in its path. Model requests carry
  prompts. A model that never uses a tool hook is not covered. See
  `docs/THREAT-LIST.md`.
- The test used a scripted model. It says nothing about how a real model behaves
  or can be talked into something.
- Hermes may skip a hook that raises. The hook catches its own errors and returns a
  block instead.

## Legacy matrix

`_legacy_classify()` and the constants around it are an older, larger rule matrix.
They exist only so `tests/test_send_lock.py` can keep running its regression cases.
They are never registered and never a fallback.

## Install

From a checkout of the repository:

```sh
hermes plugins validate plugins/send-lock
cp -r plugins/send-lock ~/.hermes/plugins/send-lock
hermes plugins enable send-lock
```

Start a new chat session afterwards; hooks load at session start. Requires Hermes
0.21.4 or newer. Platforms: macOS and Linux.

Tests (standard library only, from the repository root):

```sh
python3 tests/test_send_lock.py      # legacy matrix
python3 tests/test_one_door_plugin.py  # the registered policy
```

License: MIT (see the repository `LICENSE`).
