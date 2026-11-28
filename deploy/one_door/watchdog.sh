#!/usr/bin/env bash
# watchdog.sh: wall-clock cap for a live-model (or rehearsal) run. Plan 3c.
#
#   bash deploy/one_door/watchdog.sh <run-dir> <seconds> &
#
# Fires kill.sh when the cap passes, or at once if <run-dir>/STOP appears (an
# operator or the harness asking for a stop). Exits quietly when <run-dir>/DONE
# appears. Writes <run-dir>/WATCHDOG with what it did. Polls once a second. Exits with kill.sh's
# status, so a stop that was not verified is never reported as success.
set -uo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
run_dir=${1:?usage: watchdog.sh <run-dir> <seconds>}
seconds=${2:?usage: watchdog.sh <run-dir> <seconds>}
[[ $seconds =~ ^[0-9]{1,6}$ && $seconds -gt 0 ]] || { printf 'watchdog: seconds must be a positive integer\n' >&2; exit 2; }
start=$(date +%s)
printf '{"event":"armed","t":%s,"cap_seconds":%s,"pid":%s}\n' "$start" "$seconds" "$$" > "$run_dir/WATCHDOG"
while :; do
    if [[ -e $run_dir/DONE ]]; then
        printf '{"event":"released","t":%s}\n' "$(date +%s)" >> "$run_dir/WATCHDOG"
        exit 0
    fi
    now=$(date +%s)
    why=''
    [[ -e $run_dir/STOP ]] && why=stop-file
    (( now - start >= seconds )) && why=wall-clock-cap
    if [[ -n $why ]]; then
        printf '{"event":"fired","t":%s,"why":"%s"}\n' "$now" "$why" >> "$run_dir/WATCHDOG"
        bash "$here/kill.sh" "$run_dir" "watchdog-$why" >> "$run_dir/WATCHDOG.kill.log" 2>&1
        status=$?
        printf '{"event":"kill-done","t":%s,"rc":%s}\n' "$(date +%s)" "$status" >> "$run_dir/WATCHDOG"
        exit "$status"
    fi
    sleep 1
done
