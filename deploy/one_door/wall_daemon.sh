# shellcheck shell=bash
# Daemon modes shared by test_worker_in_wall.sh and rehearse_live.sh. Sourced, not run.
#
#   WIW_DAEMON=colima (default, macOS operator machine): one throwaway Colima VM per run,
#     unique profile, always addressed by an explicit `docker --context`, deleted after.
#   WIW_DAEMON=native (the fresh Linux test server, plan section 2): the host's own Docker
#     Engine, addressed only by its local socket. The SAME safety rules as the Colima path:
#       - Linux only, and the operator says so: WIW_NATIVE_APPROVED=yes
#       - DOCKER_HOST and DOCKER_CONTEXT must be unset (no ambient daemon choice)
#       - the daemon must be EMPTY: no containers, no images, no volumes, before anything starts
#       - the host firewall (host_firewall.sh verify: three hooks, policy drop) must already be loaded,
#         and image pulls happen only between stage-open and a verified stage-close
#       - unique compose project; full teardown: compose down --volumes, then the pulled
#         images are removed and the daemon is checked empty again (rc 3 if not)
#
# Callers provide die() and use these globals: vm vm_created native_used D pulled_images
# shellcheck disable=SC2154  # globals above are owned by the sourcing script

wall_mode() { printf '%s' "${WIW_DAEMON:-colima}"; }

wall_need_tools() {
    case $(wall_mode) in
        colima) command -v colima >/dev/null || die 'colima is required (WIW_DAEMON=colima)' ;;
        native) [[ $(uname -s) == Linux ]] || die 'WIW_DAEMON=native runs only on a Linux test host' ;;
        *) die "WIW_DAEMON must be colima or native (got $(wall_mode))" ;;
    esac
}

wall_native_guard() {
    [[ $(uname -s) == Linux ]] || die 'native daemon mode runs only on a Linux test host'
    [[ ${WIW_NATIVE_APPROVED:-} == yes ]] || die "Refusing: native mode uses this host's own Docker Engine. Set WIW_NATIVE_APPROVED=yes on a fresh test server only."
    [[ -z ${DOCKER_HOST:-} && -z ${DOCKER_CONTEXT:-} ]] || die 'Refusing: unset DOCKER_HOST and DOCKER_CONTEXT; native mode uses only the local socket'
    [[ -S /var/run/docker.sock ]] || die 'no local Docker socket at /var/run/docker.sock'
    bash "$(dirname -- "${BASH_SOURCE[0]}")/host_firewall.sh" verify || die 'Refusing: the host firewall (inet slk_wall, input/forward/output policy drop) is not loaded. Apply it before any container starts.'
}

wall_stage_open() {    # native: let the host reach the registry for the pull only. colima: nothing to do.
    [[ $(wall_mode) == native ]] || return 0
    bash "$(dirname -- "${BASH_SOURCE[0]}")/host_firewall.sh" stage-open || die 'could not open staging'
}

wall_stage_close() {   # native: close staging and prove it closed before any case runs
    [[ $(wall_mode) == native ]] || return 0
    bash "$(dirname -- "${BASH_SOURCE[0]}")/host_firewall.sh" stage-close || die 'could not close staging'
    bash "$(dirname -- "${BASH_SOURCE[0]}")/host_firewall.sh" verify-closed || die 'staging is not verified closed; refusing to run cases'
}

wall_daemon_empty() {   # refuse unless the selected daemon holds nothing at all
    [[ $("${D[@]}" ps -aq | wc -l | tr -d ' ') -eq 0 ]] || die 'daemon is not empty (containers exist); refusing'
    [[ $("${D[@]}" image ls -q | wc -l | tr -d ' ') -eq 0 ]] || die 'daemon already has images; refusing'
    if [[ $(wall_mode) == native ]]; then
        [[ $("${D[@]}" volume ls -q | wc -l | tr -d ' ') -eq 0 ]] || die 'daemon already has volumes; refusing'
    fi
}

wall_up() {   # $1 = unique suffix. Sets D (and vm for colima).
    case $(wall_mode) in
        colima)
            vm="axiom-wiw-$1"; vm_created=true
            colima start "$vm" --cpus 2 --memory 4 --disk 20 --runtime docker
            D=(docker --context "colima-$vm") ;;
        native)
            wall_native_guard
            native_used=true
            D=(docker --host unix:///var/run/docker.sock) ;;
    esac
    "${D[@]}" info >/dev/null
    wall_daemon_empty
    local engine; engine=$("${D[@]}" version --format '{{.Server.Version}}')
    [[ ${engine%%.*} =~ ^[0-9]+$ && ${engine%%.*} -ge 28 ]] || die "Docker Engine 28+ required for isolated gateway mode (got $engine)"
}

wall_daemon_ref() {   # what kill.sh needs to address the same daemon
    case $(wall_mode) in colima) printf 'colima-%s' "$vm" ;; native) printf 'unix:///var/run/docker.sock' ;; esac
}

wall_daemon_free_bytes() {   # free bytes where the daemon stores images
    case $(wall_mode) in
        colima) colima ssh --profile "$vm" -- df -Pk /var/lib/docker | awk 'NR==2 {print $4 * 1024}' ;;
        native) df -Pk "$("${D[@]}" info --format '{{.DockerRootDir}}')" | awk 'NR==2 {print $4 * 1024}' ;;
    esac
}

wall_down() {   # prints problems; returns 3 when the daemon is not clean afterwards
    local bad=0
    if ${vm_created:-false}; then
        colima delete "$vm" --force --data >/dev/null 2>&1
        if colima list 2>/dev/null | awk -v p="$vm" 'NR>1 && $1==p {found=1} END {exit !found}'; then
            printf 'TEARDOWN FAILED: Colima profile %s still exists. Delete it by hand: colima delete %s --force --data\n' "$vm" "$vm" >&2
            bad=1
        fi
        # shellcheck disable=SC2012  # best-effort leftover listing for the report
        ls -d "$HOME"/.colima/_lima/*"$vm"* "$HOME"/.colima/_lima/_disks/*"$vm"* 2>/dev/null | sed 's/^/LEFTOVER: /' >&2
    fi
    if ${native_used:-false}; then
        bash "$(dirname -- "${BASH_SOURCE[0]}")/host_firewall.sh" stage-close >/dev/null 2>&1 || bad=1
        local img
        for img in ${pulled_images[@]+"${pulled_images[@]}"}; do "${D[@]}" image rm "$img" >/dev/null 2>&1; done
        local c i v
        c=$("${D[@]}" ps -aq | wc -l | tr -d ' '); i=$("${D[@]}" image ls -q | wc -l | tr -d ' '); v=$("${D[@]}" volume ls -q | wc -l | tr -d ' ')
        if [[ $c != 0 || $i != 0 || $v != 0 ]]; then
            printf 'TEARDOWN FAILED: native daemon not empty (%s containers, %s images, %s volumes)\n' "$c" "$i" "$v" >&2
            bad=1
        fi
    fi
    [[ $bad -eq 0 ]] || return 3
}
