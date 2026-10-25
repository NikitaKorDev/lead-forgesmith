#!/usr/bin/env bash
# Optional Linux/WSL2 equivalent of ops/run.ps1. The supported host scheduler is
# Windows Task Scheduler; this script keeps a future migration cheap.
#
#   ./ops/run.sh send --limit 3 --max-runtime 720
#   ./ops/run.sh enrich --limit 30
#
# Exit 75 means another tick of the same stage is already running.
set -euo pipefail

stage="${1:-}"
if [[ -z "$stage" ]]; then
    echo "usage: $0 <source-shopify|source-olx|enrich|compose|send|backup|digest> [args...]" >&2
    exit 2
fi
shift || true

case "$stage" in
    source-shopify) profile=app;     service=app;         cmd=(forgesmith source shopify) ;;
    source-olx)     profile=browser; service=app-browser; cmd=(forgesmith source olx) ;;
    enrich)         profile=browser; service=app-browser; cmd=(forgesmith enrich) ;;
    compose)        profile=app;     service=app;         cmd=(forgesmith compose) ;;
    send)           profile=live;    service=app-live;    cmd=(forgesmith send --live --yes) ;;
    backup)         profile=app;     service=app;         cmd=(forgesmith backup --keep 22 --out /app/data/backups) ;;
    digest)         profile=app;     service=app;         cmd=(forgesmith status --json) ;;
    *)
        echo "unknown stage: $stage (approve is never scheduled)" >&2
        exit 2
        ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
mkdir -p state/logs

log="state/logs/${stage}-$(date -u +%Y%m%d).log"
last="state/last-${stage}.json"
exec 9>"${TMPDIR:-/tmp}/forgesmith-${stage}.lock"
if ! flock -n 9; then
    printf '{"stage":"%s","exit":75,"at":"%s","stop_reason":"busy"}\n' "$stage" "$(date -Is)" > "$last"
    echo "[$stage] busy: another tick is already running" >&2
    exit 75
fi

host_now="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
set +e
docker compose --profile "$profile" run --rm -T \
    -e "FORGESMITH_HOST_NOW=$host_now" \
    "$service" "${cmd[@]}" "$@" >> "$log" 2>&1
code=$?
set -e

printf '{"stage":"%s","exit":%d,"at":"%s","stop_reason":null}\n' "$stage" "$code" "$(date -Is)" > "$last"

if [[ "$stage" == "digest" && "$code" -eq 0 ]]; then
    # run.ps1 writes the digest; keep the Linux path equivalent.
    docker compose --profile app run --rm -T app forgesmith status --json > state/digest.txt 2>/dev/null || true
fi

if [[ "$code" -eq 1 || "$code" -eq 2 ]]; then
    "${root}/ops/alert.sh" "$stage" "$code" "$log" || true
fi

exit "$code"
