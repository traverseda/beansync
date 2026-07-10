FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

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

# Install beansync into a venv (avoids Debian's externally-managed-environment restriction)
WORKDIR /app
COPY . .
RUN uv venv /opt/venv && uv pip install --no-cache --python /opt/venv/bin/python .

COPY addon/run.sh /run.sh
RUN chmod +x /run.sh

# Xvfb display — browser automation runs on :99
ENV DISPLAY=:99
# Tell playwright/chromium to use the system binary
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

EXPOSE 8765

CMD ["/run.sh"]
