#!/usr/bin/env bash
set -euo pipefail

OPTIONS=/data/options.json

# Read ledger directory from HA options (falls back to /config/beansync)
LEDGER_DIR=$(jq -r '.ledger_dir // "/config/beansync"' "${OPTIONS}" 2>/dev/null || echo "/config/beansync")

# Export API keys as env vars so litellm and beansync can find them.
# beansync's secret resolver also checks SECRET_<UPPER_NAME> for any !secret refs in config.yaml.
OPENROUTER_KEY=$(jq -r '.openrouter_api_key // ""' "${OPTIONS}" 2>/dev/null || true)
ANTHROPIC_KEY=$(jq -r '.anthropic_api_key // ""'  "${OPTIONS}" 2>/dev/null || true)
[ -n "${OPENROUTER_KEY}" ] && export OPENROUTER_API_KEY="${OPENROUTER_KEY}"
[ -n "${ANTHROPIC_KEY}" ]  && export ANTHROPIC_API_KEY="${ANTHROPIC_KEY}"

# Start Xvfb so headed-mode browser automation works inside the container.
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99

mkdir -p "${LEDGER_DIR}"
cd "${LEDGER_DIR}"

# Bootstrap a skeleton ledger on first run so the UI has something to show.
if [ ! -f config.yaml ]; then
    echo "Initialising new ledger at ${LEDGER_DIR} …"
    bean-sync init .
fi

exec bean-sync serve --host 0.0.0.0 --port 8765
