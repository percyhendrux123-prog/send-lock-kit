#!/usr/bin/env bash
# Fresh Ubuntu 24.04 bootstrap. Stages a gateway; never logs in or starts it.
set -euo pipefail

usage() {
    printf '%s\n' 'Usage: install_vps.sh --non-interactive [--user hermes] --ssh-port PORT' \
        '       install_vps.sh --non-interactive --container [--user hermes]' \
        '       install_vps.sh --plan' \
        'Use only on a fresh VPS with administrator approval and console access.'
}
fail() { printf 'install_vps: %s\n' "$1" >&2; exit 2; }
require_root() { [[ $(id -u) == 0 ]] || fail 'Run as administrator on the intended VPS'; }
require_ubuntu() {
    # shellcheck disable=SC1091
    source /etc/os-release
    [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail 'Ubuntu 24.04 required'
}
memory_total_kib() {
    local key value unit
    while read -r key value unit; do
        if [[ $key == MemTotal: ]]; then printf '%s\n' "$value"; return; fi
    done < /proc/meminfo
    return 1
}
memory_limit_paths() {
    local membership=${1:-/proc/self/cgroup} root=${2:-/sys/fs/cgroup}
    local hierarchy controllers group mount name directory
    [[ -r $membership ]] || return 1
    while IFS=: read -r hierarchy controllers group; do
        if [[ -z $controllers ]]; then
            mount=$root; name=memory.max
        elif [[ ,$controllers, == *,memory,* ]]; then
            mount=$root/memory; name=memory.limit_in_bytes
        else
            continue
        fi
        # /proc supplies an absolute cgroup path. Do not escape the mounted
        # namespace; its visible root cap is checked even with a relative view.
        directory=$mount
        if [[ $group == /* && /$group/ != */../* && /$group/ != */./* ]]; then
            directory=$mount${group%/}
        fi
        while [[ $directory == "$mount"/* ]]; do
            printf '%s/%s\n' "$directory" "$name"
            directory=${directory%/*}
        done
        printf '%s/%s\n' "$mount" "$name"
    done < "$membership"
}
memory_limit_bytes() {
    local path value paths limit=max
    paths=$(memory_limit_paths "$@") || return 1
    while IFS= read -r path; do
        [[ -r $path ]] || continue
        read -r value < "$path" || return 1
        [[ $value == max ]] && continue
        [[ $value =~ ^[0-9]{1,19}$ ]] || return 1
        # Kernel v1 unlimited sentinels can approach signed-64-bit max.
        # Any 19-digit cap exceeds our RAM floor and need not be compared.
        ((${#value} < 19)) || continue
        value=$((10#$value))
        if [[ $limit == max ]] || ((value < limit)); then limit=$value; fi
    done <<< "$paths"
    printf '%s\n' "$limit"
}
require_memory() {
    local kib limit bytes
    kib=$(memory_total_kib) || fail 'Cannot read RAM; minimum 6 GB usable RAM required'
    limit=$(memory_limit_bytes) || fail 'Cannot read memory limit; minimum 6 GB usable RAM required'
    [[ $kib =~ ^[0-9]{1,12}$ && ( $limit == max || $limit =~ ^[0-9]{1,18}$ ) ]] || fail 'Cannot read RAM; minimum 6 GB usable RAM required'
    bytes=$((kib * 1024))
    if [[ $limit != max ]] && ((limit < bytes)); then bytes=$limit; fi
    ((bytes >= 6000000000)) || fail 'At least 6 GB usable RAM required for the Hermes web UI build. Increase VM/container memory before retrying; no changes made.'
}
as_user() {
    # Do not forward root's provider keys, bot tokens, proxy settings or shell rc.
    runuser -u "$SERVICE_USER" -- env -i HOME="$SERVICE_HOME" USER="$SERVICE_USER" \
        LOGNAME="$SERVICE_USER" SHELL=/bin/bash PATH=/usr/local/bin:/usr/bin:/bin \
        HERMES_HOME="$SERVICE_HOME/.hermes" "$@"
}

main() (
    set -euo pipefail
    SERVICE_USER=hermes
    local ssh_port='' container=false plan=false noninteractive=false
    local kit_dir installer uid
    kit_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    while (($#)); do
        case "$1" in
            --user|--ssh-port)
                [[ $# -ge 2 && -n $2 && $2 != --* ]] || fail 'Option needs a value'
                if [[ $1 == --user ]]; then SERVICE_USER=$2; else ssh_port=$2; fi
                shift 2 ;;
            --non-interactive) noninteractive=true; shift ;;
            --container) container=true; shift ;;
            --plan) plan=true; shift ;;
            --help|-h) usage; exit 0 ;;
            *) usage >&2; fail 'Unknown option' ;;
        esac
    done
    [[ $SERVICE_USER =~ ^[a-z][a-z0-9_-]{0,30}$ && $SERVICE_USER != root ]] || fail 'Use a non-root Unix username'
    if [[ -n $ssh_port ]]; then
        [[ $ssh_port =~ ^[0-9]{1,5}$ ]] || fail 'Invalid SSH port'
        ((10#$ssh_port >= 1 && 10#$ssh_port <= 65535)) || fail 'Invalid SSH port'
        ssh_port=$((10#$ssh_port))
    fi
    if $plan; then
        printf '%s\n' "Dedicated locked user: $SERVICE_USER" \
            'Minimum 6 GB usable RAM, including any container limit; 6 GiB / 4 CPU recommended.' \
            'Install Ubuntu prerequisites; preserve explicit SSH port before enabling ufw.' \
            'Official installer: https://hermes-agent.nousresearch.com/install.sh --non-interactive' \
            'Copy/enable send-lock; stage systemd user unit. Gateway NOT started.' \
            'Container mode skips ufw, lingering and all systemctl calls. No model/bot login.'
        exit 0
    fi
    $noninteractive || { usage >&2; fail 'Explicit --non-interactive acknowledgement required'; }
    $container || [[ -n $ssh_port ]] || fail 'Pass the actual --ssh-port; do not guess it'
    require_root
    require_ubuntu
    require_memory
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        fail 'Existing account refused; inspect existing setup and choose a new dedicated username'
    fi
    [[ -f $kit_dir/plugins/send-lock/__init__.py && -f $kit_dir/deploy/hermes-gateway.service ]] || fail 'Kit files missing'
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl git tar xz-utils unzip \
        build-essential python3 python3-venv libatomic1 ufw dbus-user-session
    useradd --create-home --shell /bin/bash --password '!' "$SERVICE_USER"
    uid=$(id -u "$SERVICE_USER")
    ((uid >= 1000)) || fail 'Refusing a system account; choose a dedicated normal user'
    SERVICE_HOME=$(getent passwd "$SERVICE_USER" | cut -d: -f6)
    [[ $SERVICE_HOME == /* && $SERVICE_HOME != / && $SERVICE_HOME != /root ]] || fail 'Unsafe service home'
    # No overwrite of an existing guard or unrelated unit; review upgrades separately.
    [[ ! -e $SERVICE_HOME/.hermes && ! -e $SERVICE_HOME/.local/bin/hermes && ! -e $SERVICE_HOME/.config/systemd/user/hermes-gateway.service ]] || fail 'Already staged; inspect existing setup instead of overwriting it'
    if ! $container; then
        ufw allow "$ssh_port/tcp"
        ufw default deny incoming
        ufw default allow outgoing
        ufw --force enable
    fi
    installer=$(mktemp)
    trap 'rm -f -- "$installer"' EXIT
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        https://hermes-agent.nousresearch.com/install.sh -o "$installer"
    chmod 0644 "$installer"
    as_user bash "$installer" --non-interactive --skip-browser --skip-computer-use
    [[ -x $SERVICE_HOME/.local/bin/hermes ]] || fail 'Official installer did not create the Hermes launcher'
    install -d -m 0700 "$SERVICE_HOME/.hermes/plugins" "$SERVICE_HOME/.hermes/plugins/send-lock" \
        "$SERVICE_HOME/.hermes/scripts" "$SERVICE_HOME/.config" "$SERVICE_HOME/.config/systemd/user"
    chown "$SERVICE_USER:" "$SERVICE_HOME/.hermes/plugins" "$SERVICE_HOME/.config"
    install -m 0600 "$kit_dir/plugins/send-lock/__init__.py" "$kit_dir/plugins/send-lock/plugin.yaml" \
        "$SERVICE_HOME/.hermes/plugins/send-lock/"
    install -m 0700 "$kit_dir/scripts/for_axiom.py" "$kit_dir/scripts/selfsend.py" \
        "$kit_dir/scripts/clickup.py" "$kit_dir/scripts/rundown.py" "$kit_dir/scripts/rundown_adapters.py" \
        "$SERVICE_HOME/.hermes/scripts/"
    install -m 0600 "$kit_dir/deploy/hermes-gateway.service" "$SERVICE_HOME/.config/systemd/user/hermes-gateway.service"
    chown -R "$SERVICE_USER:" "$SERVICE_HOME/.hermes/plugins/send-lock" "$SERVICE_HOME/.hermes/scripts" \
        "$SERVICE_HOME/.config/systemd"
    as_user "$SERVICE_HOME/.local/bin/hermes" plugins enable send-lock
    if ! $container; then
        loginctl enable-linger "$SERVICE_USER"
        systemctl start "user@$uid.service"
        as_user env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
            systemctl --user daemon-reload
    fi
    printf '%s\n' 'Staged successfully. Gateway NOT started or enabled; no model/bot login performed.' \
        'Next: follow docs/INSTALL-VPS.md for owner consent, native plugin checks and activation.'
)

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
