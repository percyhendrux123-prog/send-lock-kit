#!/usr/bin/env bash
# kill.sh: one command that stops a live-model (or rehearsal) run. Plan 3c.
#
#   bash deploy/one_door/kill.sh <run-dir> [reason]
#
# Order, each step attempted even if an earlier one fails:
#   1. stop model-proxy first (cuts the model: no further call can be made or billed)
#   2. compose kill (every container of THIS project only; volumes are kept as evidence)
#   3. drop all TEST traffic at the host firewall (native Linux host); management stays open
#      so evidence can still be pulled over SSH
#   4. write <run-dir>/KILLED with the time and reason; a killed run is PARTIAL, never a pass
#
# It reads <run-dir>/kill.env, written by the harness at stack start. Values are
# checked against a strict pattern and never executed. Safe to run twice.
set -uo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
run_dir=${1:?usage: kill.sh <run-dir> [reason]}
reason=${2:-operator}
[[ $reason =~ ^[A-Za-z0-9_.:-]{1,64}$ ]] || reason=invalid-reason
env_file=$run_dir/kill.env
[[ -f $env_file ]] || { printf 'kill.sh: %s missing; nothing known to kill\n' "$env_file" >&2; exit 2; }

mode='' docker_ref='' project='' files='' firewall=no
while IFS='=' read -r k v; do
    [[ -z $k || $k == \#* ]] && continue
    [[ ${#v} -le 512 && $v =~ ^[A-Za-z0-9_./:@,-]+$ ]] || { printf 'kill.sh: refusing unsafe value for %s\n' "$k" >&2; exit 2; }
    case $k in
        KILL_MODE) mode=$v ;;
        KILL_DOCKER) docker_ref=$v ;;
        KILL_PROJECT) project=$v ;;
        KILL_COMPOSE_FILES) files=$v ;;
        KILL_FIREWALL) firewall=$v ;;
        *) printf 'kill.sh: unknown key %s ignored\n' "$k" >&2 ;;
    esac
done < "$env_file"
[[ -n $project && -n $files ]] || { printf 'kill.sh: kill.env lacks project or compose files\n' >&2; exit 2; }
case $mode in
    colima) D=(docker --context "$docker_ref") ;;
    native) D=(docker --host "$docker_ref") ;;
    *) printf 'kill.sh: unknown mode %s\n' "$mode" >&2; exit 2 ;;
esac
compose=("${D[@]}" compose --project-name "$project")
IFS=',' read -r -a file_list <<<"$files"
for f in "${file_list[@]}"; do compose+=(-f "$f"); done

rc=0
started=$(date -u +%s)
printf 'kill.sh: %s run %s (project %s), reason %s\n' "$mode" "$run_dir" "$project" "$reason"
proxy=$("${compose[@]}" ps -q model-proxy 2>/dev/null || true)
if [[ -n $proxy ]]; then
    "${D[@]}" kill "$proxy" >/dev/null 2>&1 && printf 'step 1: model-proxy killed\n' || printf 'step 1: model-proxy already stopped\n'
else
    printf 'step 1: no model-proxy container running\n'
fi
if "${compose[@]}" kill >/dev/null 2>&1; then printf 'step 2: compose kill done (volumes kept)\n'; else printf 'step 2: compose kill reported an error (may be already stopped)\n'; fi
if [[ $firewall == yes ]]; then
    if bash "$here/host_firewall.sh" lockdown; then printf 'step 3: test traffic dropped at the host firewall\n'; else printf 'step 3: FIREWALL LOCKDOWN FAILED\n' >&2; rc=1; fi
else
    printf 'step 3: no host firewall in this mode (rehearsal host); nothing to lock\n'
fi
left=$("${compose[@]}" ps -q --status running 2>/dev/null | wc -l | tr -d ' ')
[[ $left == 0 ]] || { printf 'kill.sh: %s container(s) still running\n' "$left" >&2; rc=1; }
printf '{"t":%s,"reason":"%s","running_after":%s,"rc":%s}\n' "$started" "$reason" "${left:-0}" "$rc" > "$run_dir/KILLED"
printf 'step 4: %s written\n' "$run_dir/KILLED"
exit "$rc"
