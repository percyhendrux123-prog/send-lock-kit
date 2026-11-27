#!/usr/bin/env bash
# kill.sh: one command that stops a live-model (or rehearsal) run. Plan 3c.
#
#   bash deploy/one_door/kill.sh <run-dir> [reason]
#
# Works from a clean operator shell: it needs no Compose file, no image variables and no
# interpolation. Containers are found through the daemon named in kill.env and the Compose
# project label. Every Docker call is time-bounded, so a hung daemon cannot hold up the rest.
# Order, each step attempted even if an earlier one fails:
#   1. native Linux host: firewall lockdown first, one atomic batch (all test traffic, open
#      flows included, is dropped at once; management stays open so evidence can be pulled)
#   2. kill model-proxy (cuts the model: no further call can be made or billed)
#   3. kill every other running container of THIS project (volumes are kept as evidence)
#   4. verify nothing of the project is running; an inspection error counts as NOT verified
#   5. write <run-dir>/KILLED with the time, reason and result; a killed run is PARTIAL, never a pass
# Exit 0 only when every step succeeded and the stop was verified. Safe to run twice.
# kill.env is written by the harness at stack start; values are pattern-checked, never executed.
set -uo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
run_dir=${1:?usage: kill.sh <run-dir> [reason]}
reason=${2:-operator}
[[ ${#reason} -le 64 && $reason =~ ^[A-Za-z0-9_.:-]+$ ]] || reason=invalid-reason
env_file=$run_dir/kill.env
[[ -f $env_file ]] || { printf 'kill.sh: %s missing; nothing known to kill\n' "$env_file" >&2; exit 2; }
limit=${KILL_STEP_SECONDS:-20}
[[ $limit =~ ^[0-9]+$ ]] || limit=20

mode='' docker_ref='' project='' firewall=no
while IFS='=' read -r k v; do
    [[ -z $k || $k == \#* ]] && continue
    [[ ${#v} -le 512 && $v =~ ^[A-Za-z0-9_./:@,-]+$ ]] || { printf 'kill.sh: refusing unsafe value for %s\n' "$k" >&2; exit 2; }
    case $k in
        KILL_MODE) mode=$v ;;
        KILL_DOCKER) docker_ref=$v ;;
        KILL_PROJECT) project=$v ;;
        KILL_FIREWALL) firewall=$v ;;
        KILL_COMPOSE_FILES) : ;;   # recorded for people; not needed to stop the run
        *) printf 'kill.sh: unknown key %s ignored\n' "$k" >&2 ;;
    esac
done < "$env_file"
[[ -n $project && -n $docker_ref ]] || { printf 'kill.sh: kill.env lacks project or daemon\n' >&2; exit 2; }
case $mode in
    colima) D=(docker --context "$docker_ref") ;;
    native) D=(docker --host "$docker_ref") ;;
    *) printf 'kill.sh: unknown mode %s\n' "$mode" >&2; exit 2 ;;
esac
label="label=com.docker.compose.project=$project"

bounded() {   # seconds command... ; exit 124 when the command had to be cut off
    local secs=$1; shift
    "$@" &
    local pid=$!
    # The guard never holds the caller's output pipe open, or $(bounded ...) would wait for it.
    ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) >/dev/null 2>&1 &
    local guard=$!
    wait "$pid"
    local status=$?
    kill "$guard" 2>/dev/null
    wait "$guard" 2>/dev/null
    [[ $status -eq 137 ]] && return 124
    return "$status"
}

rc=0
started=$(date -u +%s)
printf 'kill.sh: %s daemon, project %s, reason %s\n' "$mode" "$project" "$reason"
if [[ $firewall == yes ]]; then
    if bounded "$limit" bash "$here/host_firewall.sh" lockdown; then printf 'step 1: firewall lockdown applied\n'
    else printf 'step 1: FIREWALL LOCKDOWN FAILED\n' >&2; rc=1; fi
else
    printf 'step 1: no host firewall in this mode (rehearsal host); nothing to lock\n'
fi
if proxy=$(bounded "$limit" "${D[@]}" ps -q --filter "$label" --filter label=com.docker.compose.service=model-proxy); then
    if [[ -n $proxy ]]; then
        # shellcheck disable=SC2086  # container ids split on purpose
        if bounded "$limit" "${D[@]}" kill $proxy >/dev/null; then printf 'step 2: model-proxy killed\n'
        else printf 'step 2: model-proxy kill reported an error\n' >&2; rc=1; fi
    else
        printf 'step 2: no running model-proxy\n'
    fi
else
    printf 'step 2: could not list model-proxy\n' >&2; rc=1
fi
if others=$(bounded "$limit" "${D[@]}" ps -q --filter "$label"); then
    if [[ -n $others ]]; then
        # shellcheck disable=SC2086
        if bounded "$limit" "${D[@]}" kill $others >/dev/null; then printf 'step 3: project containers killed (volumes kept)\n'
        else printf 'step 3: kill reported an error\n' >&2; rc=1; fi
    else
        printf 'step 3: no other running containers\n'
    fi
else
    printf 'step 3: could not list project containers\n' >&2; rc=1
fi
if left_ids=$(bounded "$limit" "${D[@]}" ps -q --filter "$label"); then
    left=$(printf '%s' "$left_ids" | grep -c . || true)
else
    left=unknown
fi
[[ $left == 0 ]] || { printf 'kill.sh: stop NOT verified (running: %s)\n' "$left" >&2; rc=1; }
printf '{"t":%s,"reason":"%s","running_after":"%s","rc":%s}\n' "$started" "$reason" "$left" "$rc" > "$run_dir/KILLED"
printf 'step 4-5: %s written (rc %s)\n' "$run_dir/KILLED" "$rc"
exit "$rc"
