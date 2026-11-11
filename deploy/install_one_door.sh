#!/usr/bin/env bash
# Offline, silent staging only. Root owns guard files; no account/service changes.
set -euo pipefail
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$kit/deploy/one_door/install.py" "$@" install --kit "$kit"
