#!/usr/bin/env bash
# DOCKER HOST ONLY. Fake services only. Never run this inside a build sandbox.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
base=${DOOR_BASE_IMAGE:-python:3.12-slim}
command -v docker >/dev/null || { printf 'Docker is required on the host\n' >&2; exit 2; }
docker info >/dev/null
engine=$(docker version --format '{{.Server.Version}}')
[[ ${engine%%.*} =~ ^[0-9]+$ && ${engine%%.*} -ge 28 ]] || { printf 'Docker Engine 28+ required for isolated gateway mode\n' >&2; exit 2; }
# Refuse implicit registry access. Owner stages an approved base image separately.
docker image inspect "$base" >/dev/null
project="one-door-test-$(date -u +%Y%m%d%H%M%S)-$$"
export DOOR_IMAGE="$project:local"
compose=(docker compose --project-name "$project" --profile host-test -f deploy/one_door/compose.json)
created=false
cleanup() {
    if $created; then "${compose[@]}" down --volumes --remove-orphans >/dev/null; fi
    docker image rm "$DOOR_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build --network none --pull=false --build-arg "DOOR_BASE_IMAGE=$base" \
    -t "$DOOR_IMAGE" -f deploy/one_door/Dockerfile .
"${compose[@]}" config --quiet
created=true
"${compose[@]}" run --rm --no-deps init
"${compose[@]}" up -d --pull never --no-build worker egress-gateway sender owner fake-api fake-outside fake-receiver fake-host
x() { "${compose[@]}" exec -T "$@"; }
probe() { local service=$1; shift; x "$service" python3 /kit/container_probe.py "$@"; }
# The address is discovered from THIS project's running fake outside service.
container=$("${compose[@]}" ps -q fake-outside)
ip=$(docker inspect "$container" | python3 -c 'import json,sys; n=json.load(sys.stdin)[0]["NetworkSettings"]["Networks"]; assert len(n)==1; print(next(iter(n.values()))["IPAddress"])')
ready=false
for attempt in {1..20}; do
    if probe sender control "$ip" >/dev/null 2>&1 && x worker python3 -c \
        'import urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); o.open("http://egress-gateway:8080/google/read/mail", timeout=2).close()' >/dev/null 2>&1 && probe sender url-control 'http://fake-receiver:8080/control' >/dev/null 2>&1; then ready=true; break; fi
    sleep 1
done
$ready || { "${compose[@]}" logs; printf 'Fake controls not ready; no isolation claim\n' >&2; exit 1; }
probe sender control "$ip"
probe worker boundary "$ip"
# An internal bridge alone can still reach host-bound services. Require Docker's
# isolated gateway mode and test an actual disposable host-network listener.
network=$(docker inspect "$container" | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin)[0]["NetworkSettings"]["Networks"].values()))["NetworkID"])')
host_ip=$(docker network inspect "$network" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["IPAM"]["Config"][0]["Gateway"])')
worker_id=$("${compose[@]}" ps -q worker)
worker_network=$(docker inspect "$worker_id" | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin)[0]["NetworkSettings"]["Networks"].values()))["NetworkID"])')
worker_gateway=$(docker network inspect "$worker_network" | python3 -c 'import json,sys; n=json.load(sys.stdin)[0]; assert n["Internal"] and n["Options"]["com.docker.network.bridge.gateway_mode_ipv4"]=="isolated"; print(n["IPAM"]["Config"][0]["Gateway"])')
host_id=$("${compose[@]}" ps -q fake-host)
host_ready=false
for attempt in {1..20}; do
    port=$(docker logs "$host_id" | python3 -c 'import sys; print(next((line.strip().split("=",1)[1] for line in sys.stdin if line.startswith("FAKE_PORT=")),""))')
    if [[ $port =~ ^[0-9]+$ ]] && probe sender url-control "http://$host_ip:$port/control" >/dev/null 2>&1; then host_ready=true; break; fi
    sleep 1
done
$host_ready || { printf 'Host-listener control unavailable; no host-boundary claim\n' >&2; exit 1; }
probe sender url-control "http://$host_ip:$port/control"
probe worker url-denied "http://$host_ip:$port/control"
probe worker url-denied "http://$worker_gateway:$port/control"

step() { x owner python3 /kit/owner.py step "$1"; }
approve() {
    local digest
    digest=$(probe owner hash "$1")
    x owner python3 /kit/owner.py approve "$1" --sha256 "$digest"
}
expect_send_state() {
    local expected=$1 filename=$2 actual
    actual=$(probe sender send "$filename")
    [[ $actual == "$expected" ]] || { printf 'Expected %s, got %s\n' "$expected" "$actual" >&2; exit 1; }
    printf 'PASS sender %s: %s\n' "$filename" "$actual"
}
step 3
probe worker queue first.json
expect_send_state hold first.json
probe fake-receiver receipts 0
approve first.json
expect_send_state send first.json
probe fake-receiver receipts 1
expect_send_state refuse first.json
probe worker queue edited.json
approve edited.json
probe worker queue edited.json --body 'edited after owner approval'
expect_send_state refuse edited.json
probe fake-receiver receipts 1
step 2
probe worker queue step-two.json
approve step-two.json
expect_send_state hold step-two.json
probe fake-receiver receipts 1
probe sender audit '["hold","attempt","send","refuse","refuse","hold"]'
printf 'One-door Docker integration: PASS (fake services only)\n'
