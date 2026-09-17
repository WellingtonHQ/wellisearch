#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== reindex: recreating container so .env changes take effect =="
docker compose -f compose.yml up -d --force-recreate wellisearch

echo "== waiting for container readiness (30 checks, 2s apart) =="
ready=0
for _attempt in $(seq 1 30); do
    if docker compose -f compose.yml exec wellisearch python -m wellisearch.reindex --dry-run >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done

if [ "$ready" -ne 1 ]; then
    echo "wellisearch did not become ready in time." >&2
    echo "Check the container logs with: docker compose -f compose.yml logs wellisearch" >&2
    exit 1
fi

echo "== re-chunking and re-embedding every page; this can take a while =="
docker compose -f compose.yml exec wellisearch python -m wellisearch.reindex --force
