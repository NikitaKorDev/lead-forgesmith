#!/usr/bin/env bash
# Best-effort Linux/WSL2 alert hook used by ops/run.sh. Never fails the stage.
set -u

stage="${1:-unknown}"
code="${2:-1}"
log="${3:-}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${root}/state"
message="forgesmith: ${stage} exit ${code} (log ${log})"

if [[ -n "${FORGESMITH_NTFY_URL:-}" ]]; then
    curl -fsS -X POST -H "Title: forgesmith: ${stage} exit ${code}" \
        -H "Tags: warning" --data-binary "$message" "$FORGESMITH_NTFY_URL" >/dev/null || true
elif [[ -n "${FORGESMITH_ALERT_WEBHOOK:-}" ]]; then
    curl -fsS -X POST -H "Content-Type: application/json" \
        --data-binary "{\"text\":\"${message}\",\"stage\":\"${stage}\",\"exit\":${code}}" \
        "$FORGESMITH_ALERT_WEBHOOK" >/dev/null || true
else
    printf '[%s] %s\n' "$(date -Is)" "$message" >> "${root}/state/alerts.log"
fi

exit 0
