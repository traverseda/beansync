#!/usr/bin/env bash
# Build the actual add-on Docker image and run the ingress test harness against
# it. This is what "verified locally" means: the shipped artifact, with the
# locked dependency versions, answering supervisor-style requests correctly.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=beansync-addon-test
CONTAINER=beansync-addon-test
PORT="${PORT:-18765}"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== building image =="
docker build -f "$REPO_ROOT/addon/Dockerfile" -t "$IMAGE" "$REPO_ROOT"

echo "== starting container =="
cleanup
docker run -d --name "$CONTAINER" -p "127.0.0.1:${PORT}:8765" "$IMAGE"

echo "== waiting for server =="
for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${PORT}/_debug/ingress" >/dev/null 2>&1; then
        break
    fi
    if [ "$i" = 60 ]; then
        echo "server did not come up; container logs:" >&2
        docker logs "$CONTAINER" >&2
        exit 1
    fi
    sleep 1
done

echo "== running ingress tests =="
"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/test_ingress.py" "http://127.0.0.1:${PORT}"
