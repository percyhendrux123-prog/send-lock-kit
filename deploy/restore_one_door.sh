#!/usr/bin/env bash
# One-step restoration of an owner-selected dated staging backup.
set -euo pipefail
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$kit/deploy/one_door/install.py" "$@"
