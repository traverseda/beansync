FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
# Stdout/stderr are block-buffered when piped (always true under Docker), so
# a hard kill (e.g. OOM) can drop everything sitting in the buffer before it
# ever reaches `docker logs`. Unbuffered output means a crash still leaves
# whatever was logged right before it.
ENV PYTHONUNBUFFERED=1

# System packages: Python, Node.js source, Xvfb, Chromium, git, jq
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    curl ca-certificates gnupg \
    xvfb \
    chromium \
    chromium-driver \
    fonts-liberation fonts-noto-color-emoji \
    libasound2 \
    libgbm1 \
    git \
    openssh-client \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (needed for npx tsx / stagehand scripts)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:/opt/venv/bin:$PATH"

# tsx for running stagehand scripts via npx
RUN npm install -g tsx

# Install beansync into a venv (avoids Debian's externally-managed-environment restriction).
# uv sync --locked installs the exact versions from uv.lock, so the image runs
# the same dependency versions that were tested locally.
WORKDIR /app
COPY . .
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --locked --no-dev --no-editable --no-cache

# CUA sources (beansync/sync_cua.py) drive a real Firefox via Playwright —
# the `playwright` Python package only ships the driver, not a browser
# binary, so it has to be fetched separately. --with-deps also pulls in the
# system shared libs Firefox needs (distinct from the Chromium ones above).
RUN playwright install --with-deps firefox

COPY addon/run.sh /run.sh
RUN chmod +x /run.sh

# Xvfb display — browser automation runs on :99
ENV DISPLAY=:99
# Tell playwright/chromium to use the system binary
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

EXPOSE 8765

CMD ["/run.sh"]
