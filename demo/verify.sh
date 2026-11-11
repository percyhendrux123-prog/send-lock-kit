#!/usr/bin/env bash
# Shared offline verification path: the first argument labels the real executor.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONDONTWRITEBYTECODE=1
printf 'Fake-data verification | executor: %s\n' "${1:-direct-shell}"
printf 'No model login, bot token, Google/ClickUp/Discord calls or sends.\n\n'
python3 scripts/rundown.py --fixtures demo/fixtures --now 2026-11-10T08:00:00-06:00
printf '\nOffline test suites\n'
for suite in tests/test_*.py; do
    printf '\n$ python3 %s\n' "$suite"
    python3 "$suite"
done
printf '\n$ bash -n (all kit shell scripts)\n'
shopt -s nullglob
scripts=(deploy/*.sh deploy/one_door/*.sh demo/*.sh)
for script in "${scripts[@]}"; do bash -n "$script"; printf 'bash -n %s: PASS\n' "$script"; done
if command -v shellcheck >/dev/null; then
    shellcheck "${scripts[@]}"
    printf 'shellcheck: PASS\n'
else
    printf 'shellcheck: unavailable (not run)\n'
fi
printf 'Offline verification: PASS\n'
