#!/usr/bin/env bash
# Owner-operated host wrapper. No credentials mounted or forwarded.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
started_colima=false
output_tmp=''
cleanup() {
    local rc=$?
    [[ -z $output_tmp ]] || rm -f -- "$output_tmp"
    if $started_colima; then colima stop || rc=1; fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
command -v docker >/dev/null || { printf 'Docker CLI is required.\n' >&2; exit 1; }
if ! docker info >/dev/null 2>&1; then
    if [[ $(uname -s) == Darwin ]] && command -v colima >/dev/null; then
        # Fail closed on a failed/unknown status. Inspect the successful JSON
        # inventory rather than treating every status error as a stopped VM.
        command -v python3 >/dev/null || { printf 'python3 required for safe Colima status parsing.\n' >&2; exit 1; }
        profiles=$(colima list --json) || { printf 'Cannot inspect Colima; left untouched.\n' >&2; exit 1; }
        printf '%s\n' "$profiles" | python3 -c '
import json, sys
try:
    rows = [json.loads(line) for line in sys.stdin if line.strip()]
    if len(rows) == 1 and isinstance(rows[0], list):
        rows = rows[0]
    if any(not isinstance(r, dict) or r.get("status", "").lower() != "stopped" for r in rows):
        raise ValueError("running or unknown VM")
except (ValueError, TypeError, AttributeError):
    sys.exit("Colima inventory is running or unknown; left untouched.")
' || exit 1
        # Only a successful start establishes cleanup ownership. A failed start
        # is ambiguous: do not issue an unowned stop; ask the operator to inspect.
        colima start || { printf 'Colima start failed; inspect VM state manually.\n' >&2; exit 1; }
        started_colima=true
        docker info >/dev/null
    else
        printf 'Docker daemon is not answering. Start it explicitly, then retry.\n' >&2
        exit 1
    fi
fi
docker build -f "$root/demo/Dockerfile" -t axiom-bot-setup-kit-demo "$root"
output_tmp=$(mktemp "$root/demo/.OUTPUT.XXXXXX")
# The runtime has no network, sockets, secrets, host mounts or elevated capabilities.
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
    axiom-bot-setup-kit-demo 2>&1 | tee "$output_tmp"
mv -- "$output_tmp" "$root/demo/OUTPUT.txt"
output_tmp=''
printf 'Saved real Docker runtime output to demo/OUTPUT.txt\n'
