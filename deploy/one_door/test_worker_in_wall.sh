#!/usr/bin/env bash
# DOCKER HOST ONLY. Fake services only. Never run this inside a build sandbox.
#
# Worker-in-wall acceptance test: a real Hermes runtime inside the one-door wall,
# driven by a scripted fake model, asked to do each of the 46 should-block probes,
# judged only from evidence collected outside the worker.
# Plan and limits: docs/WORKER-IN-WALL-PLAN.md. Results of full runs: docs/evidence/.
#
#   bash deploy/one_door/test_worker_in_wall.sh help       print this summary
#   bash deploy/one_door/test_worker_in_wall.sh preflight  host checks only, changes nothing
#   WIW_APPROVED_RUN=yes WIW_PULL_APPROVED=yes \
#   WIW_BASE_IMAGE=python@sha256:<digest> WIW_HERMES_IMAGE=nousresearch/hermes-agent@sha256:<digest> \
#     bash deploy/one_door/test_worker_in_wall.sh all 2>&1 | tee /tmp/wiw-run.txt
#
# What `all` does, in order (every step aborts the run on failure; teardown always runs):
#   preflight: >= 10 GiB free, no container and no VM already running.
#   Creates ONE throwaway Colima VM (unique profile) and uses only that daemon, always
#   through an explicit `docker --context`. It never touches another VM or daemon.
#   Stage: the ONLY network use. Pulls exactly two digest-pinned images into the VM.
#   Everything after that uses --pull never / --pull=false / build network none.
#   Smoke (no model-driven attacks), then phase "locked" (plugin on), phase "wall-only"
#   (plugin off), then the approval flow. Evaluate. Teardown. Free-space check.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
root=$PWD
check=deploy/one_door/wiw_check.py
cases=deploy/one_door/wiw_cases.json
compose_file=deploy/one_door/compose.worker-in-wall.json
hermes_bin=/opt/hermes/.venv/bin/hermes
hermes_py=/opt/hermes/.venv/bin/python

die() { printf '%s\n' "$*" >&2; exit 2; }
py() { python3 "$check" "$@"; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d'; }

need_tools() {
    local tool
    for tool in python3 docker colima awk; do command -v "$tool" >/dev/null || die "$tool is required on the host"; done
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die 'Python 3.11+ is required'
}

free_bytes() { python3 -c 'import shutil,sys; print(shutil.disk_usage(sys.argv[1]).free)' "$HOME"; }

preflight() {
    need_tools
    local containers vms
    containers=$(docker ps --format '{{.Names}}' 2>/dev/null || true)
    vms=$( { colima list 2>/dev/null | awk 'NR>1 && $2=="Running" {print "colima profile " $1}'
             pgrep -fl 'limactl|qemu-system|Virtualization.VirtualMachine|com.docker.backend|com.docker.virtualization' 2>/dev/null || true; } )
    # Operator override: WIW_TOLERATE_VM_PIDS="pid pid ..." names processes of ANOTHER VM the operator
    # has identified and accepts running beside this test (for example the builder sandbox VM).
    # Only those exact pids are dropped from the refusal list; each one is printed. Nothing is stopped.
    if [[ -n ${WIW_TOLERATE_VM_PIDS:-} ]]; then
        [[ $WIW_TOLERATE_VM_PIDS =~ ^[0-9\ ]+$ ]] || die 'WIW_TOLERATE_VM_PIDS takes process ids only (digits and spaces)'
        printf 'preflight: operator tolerates VM pids: %s\n' "$WIW_TOLERATE_VM_PIDS"
        vms=$(printf '%s\n' "$vms" | awk -v keep=" $WIW_TOLERATE_VM_PIDS " 'index(keep, " " $1 " ") == 0')
    fi
    py preflight --path "$HOME" --containers-text "$containers" --vms-text "$vms" || exit 2
    py validate-cases --cases "$cases" --probe tests/fixtures/attack_cases/probe.py || exit 2
}

# ---- state used by teardown -------------------------------------------------
vm=''; project=''; vm_created=false; stack_created=false; before_free=0; final_rc=0
D=(); compose=(); evid=''; stage=''

teardown() {
    local rc=$?
    trap - EXIT
    set +e
    [[ $final_rc -ne 0 ]] && rc=$final_rc
    if $stack_created; then
        "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1
        "${D[@]}" image rm "${derived:-wiw-none}" "${kit:-wiw-none}" >/dev/null 2>&1
    fi
    [[ -n $stage && -d $stage ]] && rm -rf -- "$stage"
    if $vm_created; then
        colima delete "$vm" --force --data >/dev/null 2>&1
        if colima list 2>/dev/null | awk -v p="$vm" 'NR>1 && $1==p {found=1} END {exit !found}'; then
            printf 'TEARDOWN FAILED: Colima profile %s still exists. Delete it by hand: colima delete %s --force --data\n' "$vm" "$vm" >&2
            rc=3
        fi
        ls -d "$HOME"/.colima/_lima/*"$vm"* "$HOME"/.colima/_lima/_disks/*"$vm"* 2>/dev/null | sed 's/^/LEFTOVER: /' >&2
    fi
    if [[ $before_free -gt 0 ]]; then
        python3 "$check" space-returned --before "$before_free" --after "$(free_bytes)" || { printf 'Free space did not return. Colima keeps a download cache outside the VM; inspect it by hand.\n' >&2; [[ $rc -eq 0 ]] && rc=3; }
    fi
    exit "$rc"
}

main_all() {
    [[ ${WIW_APPROVED_RUN:-} == yes ]] || die 'Refusing: set WIW_APPROVED_RUN=yes (the operator approves this run)'
    [[ ${WIW_PULL_APPROVED:-} == yes ]] || die 'Refusing: set WIW_PULL_APPROVED=yes. The stage step pulls two pinned images from the registry; that is the only network use.'
    base=${WIW_BASE_IMAGE:?set WIW_BASE_IMAGE=python@sha256:<digest> (the base for the fake services)}
    hermes=${WIW_HERMES_IMAGE:?set WIW_HERMES_IMAGE=nousresearch/hermes-agent@sha256:<digest>}
    need_tools
    py digest "$base" || exit 2
    py digest "$hermes" || exit 2
    preflight
    before_free=$(free_bytes)

    local stamp; stamp=$(date -u +%Y%m%d%H%M%S)
    vm="axiom-wiw-$stamp-$$"; project="wiw-$stamp-$$"
    evid=${WIW_EVIDENCE_DIR:-$root/docs/evidence/worker-in-wall-$stamp}
    mkdir -p "$evid/smoke" "$evid/approval" "$evid/locked" "$evid/wall-only"
    D=(docker --context "colima-$vm")
    kit="$project-kit:local"; derived="$project-door:local"
    export DOOR_IMAGE=$derived WIW_HERMES_IMAGE=$hermes
    compose=("${D[@]}" compose --project-name "$project" -f "$compose_file")
    trap teardown EXIT
    trap 'exit 130' INT TERM
    printf 'wiw start %s | vm %s | project %s | evidence %s\n' "$stamp" "$vm" "$project" "$evid"

    # ---- throwaway VM -------------------------------------------------------
    vm_created=true
    colima start "$vm" --cpus 2 --memory 4 --disk 20 --runtime docker
    "${D[@]}" info >/dev/null
    [[ $("${D[@]}" ps -aq | wc -l | tr -d ' ') -eq 0 ]] || die 'throwaway daemon is not empty; refusing'
    [[ $("${D[@]}" image ls -q | wc -l | tr -d ' ') -eq 0 ]] || die 'throwaway daemon already has images; refusing'
    local engine; engine=$("${D[@]}" version --format '{{.Server.Version}}')
    [[ ${engine%%.*} =~ ^[0-9]+$ && ${engine%%.*} -ge 28 ]] || die "Docker Engine 28+ required for isolated gateway mode (got $engine)"
    local vmfree; vmfree=$(colima ssh --profile "$vm" -- df -Pk /var/lib/docker | awk 'NR==2 {print $4 * 1024}')
    py vm-disk --free-bytes "$vmfree" || exit 2

    # ---- stage: the only network use ---------------------------------------
    printf 'STAGE (network): pulling two pinned images into the throwaway VM only\n'
    "${D[@]}" pull "$base"
    "${D[@]}" pull "$hermes"
    "${D[@]}" image inspect --format '{{.Id}} arch={{.Architecture}} user={{.Config.User}} entrypoint={{json .Config.Entrypoint}}' "$hermes" | tee "$evid/hermes-image.txt"
    py image --bytes "$("${D[@]}" image inspect --format '{{.Size}}' "$hermes")" || exit 2
    # Layout check 1: the image runs as the worker UID with no capabilities, read-only root, no network.
    "${D[@]}" run --rm --pull never --network none --user 10001:10002 --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true -e HERMES_HOME=/opt/data \
        --tmpfs /opt/data:rw,nosuid,nodev,size=64m,uid=10001,gid=10002 --tmpfs /tmp:rw,nosuid,nodev,size=16m,uid=10001,gid=10002 \
        --entrypoint "$hermes_bin" "$hermes" --version | tee "$evid/hermes-version.txt" \
        || die 'LAYOUT BLOCKER: the Hermes image does not start as uid 10001, read-only, no capabilities. See docs/WORKER-IN-WALL-PLAN.md section 1.'

    # ---- build (network none, no pulls) ------------------------------------
    "${D[@]}" build --network none --pull=false --build-arg "DOOR_BASE_IMAGE=$base" -t "$kit" -f deploy/one_door/Dockerfile .
    stage=$(mktemp -d)
    cp deploy/one_door/fake_model.py deploy/one_door/tripwire.py "$cases" "$stage/"
    cp deploy/one_door/config.worker-in-wall.yaml "$stage/config.yaml"
    cp deploy/one_door/config.worker-in-wall.wall-only.yaml "$stage/config.wall-only.yaml"
    cp deploy/one_door/egress.worker-in-wall.json "$stage/egress.json"
    chmod 0644 "$stage"/*
    cat > "$stage/Dockerfile" <<'EOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY fake_model.py tripwire.py wiw_cases.json config.yaml config.wall-only.yaml egress.json /kit/
USER 10001:10002
CMD ["sleep", "infinity"]
EOF
    "${D[@]}" build --network none --pull=false --build-arg "BASE_IMAGE=$kit" -t "$derived" "$stage"

    # ---- stack --------------------------------------------------------------
    "${compose[@]}" config --quiet
    stack_created=true
    "${compose[@]}" run --rm --no-deps init
    "${compose[@]}" run --rm --no-deps init-canary
    "${compose[@]}" up -d --pull never --no-build worker egress-gateway sender owner fake-model tripwire fake-receiver

    x() { "${compose[@]}" exec -T "$@"; }
    probe() { local service=$1; shift; x "$service" python3 /kit/container_probe.py "$@"; }
    mark() { local u; for u in fake-model tripwire; do probe sender url-control "http://$u:8080/__mark/$1" >/dev/null; done; }
    net_ip() { "${D[@]}" inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{$v.IPAddress}}{{"\n"}}{{end}}' "$("${compose[@]}" ps -q "$1")" | awk -v pat="$2" '$1 ~ pat {print $2}'; }
    trip_ip=$(net_ip tripwire 'outside-net$')
    gw_ip=$(net_ip egress-gateway 'outside-net$')
    [[ -n $trip_ip && -n $gw_ip ]] || die 'could not discover tripwire and gateway addresses'
    printf '%s\n%s\n' "tripwire=$trip_ip" "gateway_outside=$gw_ip" > "$evid/addresses.txt"

    ready=false
    for _ in {1..30}; do
        if probe sender url-control "http://fake-model:8080/__health" >/dev/null 2>&1 && probe sender control "$trip_ip" >/dev/null 2>&1; then ready=true; break; fi
        sleep 1
    done
    $ready || { "${compose[@]}" logs --no-color --tail 100 >&2; die 'Positive controls not ready; no isolation claim'; }

    snap() {
        local name=$1
        x fake-receiver sh -c 'if [ -f /receipts/received.jsonl ]; then cat /receipts/received.jsonl; fi' > "$evid/receipts-after-$name.jsonl"
        x sender sh -c 'if [ -f /data/state/audit.jsonl ]; then cat /data/state/audit.jsonl; fi' > "$evid/audit-after-$name.jsonl"
    }
    collect_logs() {
        x fake-model sh -c 'if [ -f /canary/model-log.jsonl ]; then cat /canary/model-log.jsonl; fi' > "$evid/model-log.jsonl"
        x tripwire sh -c 'if [ -f /canary/tripwire-log.jsonl ]; then cat /canary/tripwire-log.jsonl; fi' > "$evid/tripwire-log.jsonl"
    }
    run_case() {   # id output-dir. The agent's own output is kept for people; the checker ignores it.
        local id=$1 out=$2 path
        path=$(py planted --cases "$cases" --id "$id" --field path)
        if [[ -n $path ]]; then
            py planted --cases "$cases" --id "$id" --field text | x worker sh -c 'mkdir -p /workspace/fixtures/notes && cat > "$1"' sh "$path"
        fi
        py prompt --cases "$cases" --id "$id" | x worker timeout 420 "$hermes_bin" chat -Q --yolo --accept-hooks \
            -t terminal,file,skills,todo,tts --max-turns 8 --run-budget 400 --query-file - >"$out/$id.out" 2>"$out/$id.err" \
            || printf '%s exit=%s\n' "$id" "$?" >> "$out/exit-codes.txt"
    }
    phase() {      # name
        local name=$1
        py floor --path "$HOME" || exit 2
        mark "phase:$name:start"
        local id
        # A non-probe case (the route control) makes one permitted read on purpose. The checker counts any
        # tripwire event within 1 s before / 5 s after a case as that case's, so keep it 7 s clear of its
        # neighbours; run 3 charged the control's own read to P48 and P49.
        while IFS= read -r id; do
            if [[ $id != P* ]]; then sleep 7; run_case "$id" "$evid/$name"; sleep 7; else run_case "$id" "$evid/$name"; fi
        done < <(py ids --cases "$cases" --set probes)
        mark "phase:$name:end"
        snap "$name"
        x worker sh -c 'cat /opt/data/logs/send-lock.log 2>/dev/null; true' > "$evid/send-lock-after-$name.log"
        if [[ $name == wall-only ]]; then
            local a b
            a=$(grep -c . "$evid/send-lock-after-locked.log" || true); b=$(grep -c . "$evid/send-lock-after-wall-only.log" || true)
            [[ $a == "$b" ]] || { collect_logs; die "wall-only phase is not wall-only: the send-lock wrote $((b - a)) line(s) during it. No verdict."; }
        fi
        collect_logs   # keep the model and tripwire logs even if a later step aborts the run
    }
    set_mode() {   # locked | wall-only. The owner container rewrites the file in place (same inode).
        if [[ $1 == wall-only ]]; then x owner cp /kit/config.wall-only.yaml /data/guard/config.yaml; else x owner cp /kit/config.yaml /data/guard/config.yaml; fi
        local seen; seen=$(x worker cat /opt/data/config.yaml)
        if [[ $1 == wall-only ]]; then grep -q 'enabled: \[\]' <<<"$seen" || die 'worker does not see the wall-only config'
        else grep -q 'enabled: \[send-lock\]' <<<"$seen" || die 'worker does not see the locked config'; fi
        # Run 4: the plugin log showed ONE load (smoke) yet refusals in every later process, wall-only included,
        # so something long-lived in the worker keeps the hooks it loaded. Record what is running, stop every
        # worker process except pid 1 and its sleep, and record again.
        x worker "$hermes_py" -c '
import os, signal, sys
me = {os.getpid(), os.getppid()}
def procs():
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try: out.append((int(d), open("/proc/%s/cmdline" % d, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace").strip()))
            except OSError: pass
    return sorted(out)
before = procs()
for pid, cmd in before:
    if pid == 1 or pid in me or cmd.startswith("sleep ") or "docker-init" in cmd or not cmd: continue
    try: os.kill(pid, signal.SIGKILL)
    except OSError as e: print("kill failed", pid, e)
import time; time.sleep(1)
print("BEFORE"); [print(p, c[:200]) for p, c in before]
print("AFTER"); [print(p, c[:200]) for p, c in procs()]
' > "$evid/worker-procs-$1.txt" 2>&1 || true
        # Hermes itself must agree: "/" as a safe root and "enabled: []" alone both failed silently in runs 1 and 2.
        x worker "$hermes_bin" plugins list > "$evid/plugins-$1.txt" 2>&1 || true
        if [[ $1 == wall-only ]]; then
            if grep -Eq 'send-lock[^│]*│ *enabled' "$evid/plugins-$1.txt"; then die 'wall-only phase refused: Hermes still lists send-lock as enabled'; fi
        else grep -Eq 'send-lock[^│]*│ *enabled' "$evid/plugins-$1.txt" || die 'locked phase refused: Hermes does not list send-lock as enabled'; fi
    }

    # ---- smoke: layout and model route, before any attack case ---------------
    # The worker probe makes one permitted GET to the tripwire; it runs BEFORE the smoke window.
    x worker "$hermes_py" - "$trip_ip" < deploy/one_door/wiw_worker_probe.py > "$evid/smoke/worker-probe.json" || true
    { x worker "$hermes_bin" --version; x worker "$hermes_bin" plugins list; } > "$evid/smoke/hermes-plugins.txt" 2>&1 || true
    mark phase:smoke:start
    run_case SMOKE "$evid/smoke"
    mark phase:smoke:end
    collect_logs
    py smoke --evidence "$evid" --cases "$cases" --gateway-ip "$gw_ip" | tee "$evid/smoke/verdict.txt" \
        || die 'SMOKE did not pass. Nothing further was run. Read smoke/ in the evidence folder.'

    # ---- phases -----------------------------------------------------------
    set_mode locked;    phase locked
    set_mode wall-only; phase wall-only
    set_mode locked

    # ---- approval flow (plugin on, owner sets steps from outside) -------------
    py floor --path "$HOME" || exit 2
    local d=$evid/approval
    send() { local result; result=$(probe sender send "$1"); printf '%s %s\n' "$1" "$result" >> "$d/send-results.txt"; }
    approve() {    # file ; prints the digest the owner inspected
        local digest; digest=$(probe owner hash "$1")
        x owner python3 /kit/owner.py approve "$1" --sha256 "$digest"
        printf '%s' "$digest"
    }
    : > "$d/send-results.txt"
    mark phase:approval:start
    x owner python3 /kit/owner.py step 3
    run_case A1 "$d"
    send first.json
    run_case A2 "$d"
    x owner ls -1 /data/approvals > "$d/approvals-after-forge.txt"
    send first.json
    x owner cat /data/outbox/first.json > "$d/draft-first.json"
    approve first.json > "$d/owner-digest-first.txt"
    x owner cat /data/approvals/first.json > "$d/approval-first.json"
    send first.json
    send first.json
    run_case A3 "$d"
    approve edited.json >/dev/null
    x owner cat /data/outbox/edited.json > "$d/draft-edited-approved.json"
    run_case A4 "$d"
    x owner cat /data/outbox/edited.json > "$d/draft-edited-final.json"
    send edited.json
    x owner python3 /kit/owner.py step 2
    run_case A5 "$d"
    approve step-two.json >/dev/null
    send step-two.json
    x owner ls -1 /data/approvals > "$d/approvals-listing.txt"
    x sender cat /data/state/audit.jsonl > "$d/audit.jsonl"
    x fake-receiver sh -c 'if [ -f /receipts/received.jsonl ]; then cat /receipts/received.jsonl; fi' > "$d/receipts-final.jsonl"
    mark phase:approval:end

    # ---- evidence and verdict --------------------------------------------
    collect_logs
    x worker sh -c 'cat /opt/data/logs/send-lock.log 2>/dev/null; true' > "$evid/worker-send-lock.log"
    x worker sh -c 'cat /opt/data/logs/errors.log 2>/dev/null | tail -n 200; true' > "$evid/worker-errors-tail.log"
    "${D[@]}" inspect "$("${compose[@]}" ps -q worker)" > "$evid/inspect-worker.json"
    "${D[@]}" network inspect "${project}_worker-net" > "$evid/network-worker-net.json"
    "${compose[@]}" logs --no-color --tail 200 egress-gateway fake-model tripwire sender > "$evid/service-logs.txt" 2>&1 || true
    set +e
    py evaluate --evidence "$evid" --cases "$cases" --gateway-ip "$gw_ip" | tee "$evid/verdict.txt"
    final_rc=${PIPESTATUS[0]}
    set -e
    printf 'Evidence: %s\nTearing down (project %s, VM %s)...\n' "$evid" "$project" "$vm"
}

case "${1:-help}" in
    help|-h|--help) usage ;;
    preflight) preflight ;;
    all) main_all ;;
    *) die "unknown command: $1 (use help)" ;;
esac
