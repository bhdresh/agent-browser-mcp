# =============================================================================
# agent-browser MCP Server (Streamable-HTTP)
# =============================================================================
# Single-stage Docker image that:
#   1. Installs agent-browser CLI + Chrome (headless) + system deps
#   2. Installs Python MCP server (fastmcp)
#   3. Starts the MCP server on configurable port (default 8010)
#
# Usage:
#   docker build -t agent-browser-mcp .
#
#   docker run -d \
#     --name agent-browser-mc \
#     --restart unless-stopped \
#     -p 8010:8010 \
#     -v /tmp/:/tmp/ \
#     -v agent-browser-data:/root/.agent-browser \
#     -e AGENT_BROWSER_ENGINE=chrome \
#     -e AGENT_BROWSER_IGNORE_HTTPS_ERRORS=true \
#     -e AGENT_BROWSER_SCREENSHOT_DIR=/tmp/ \
#     -e MCP_PORT=8010 \
#     -e MCP_HOST=0.0.0.0 \
#     -e BEARER_TOKEN=your-secret-token \
#     agent-browser-mcp
# =============================================================================

FROM python:3.12-slim AS base

# Avoid interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# ──────────────────────────────────────────────────────────────
# 1. Install system dependencies for headless Chrome
# ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core system libs
    ca-certificates \
    curl \
    wget \
    gnupg \
    # Node.js + npm (required for agent-browser CLI)
    nodejs \
    npm \
    # Chrome dependencies
    fonts-liberation \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxshmfence1 \
    libxss1 \
    libxtst6 \
    # Xvfb for virtual framebuffer (headed mode if needed)
    xvfb \
    # Utilities
    procps \
    xdg-utils \
    # Clean up
    && rm -rf /var/lib/apt/lists/*

# ──────────────────────────────────────────────────────────────
# 2. Install agent-browser CLI globally
# ──────────────────────────────────────────────────────────────
RUN npm install -g agent-browser

# Download Chrome for Testing + install system deps
RUN agent-browser install --with-deps

# ──────────────────────────────────────────────────────────────
# 3. Install Python MCP server dependencies
# ──────────────────────────────────────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ──────────────────────────────────────────────────────────────
# 4. Copy the MCP server code
# ──────────────────────────────────────────────────────────────
WORKDIR /app
COPY agent_browser_mcp_server.py /app/agent_browser_mcp_server.py

# ──────────────────────────────────────────────────────────────
# 5. Environment defaults
# ──────────────────────────────────────────────────────────────
# agent-browser settings
ENV AGENT_BROWSER_ENGINE=chrome
ENV AGENT_BROWSER_IGNORE_HTTPS_ERRORS=true
ENV AGENT_BROWSER_SCREENSHOT_DIR=/tmp/
ENV AGENT_BROWSER_HEADLESS=true

# MCP server settings
ENV MCP_PORT=8010
ENV MCP_HOST=0.0.0.0
ENV CLI_TIMEOUT=60

# No Docker-in-Docker — agent-browser runs directly in this container
ENV DOCKER_CONTAINER_NAME=""

# Bearer token authentication (leave empty to disable)
ENV BEARER_TOKEN=""

# ──────────────────────────────────────────────────────────────
# 6. Expose port & start MCP server
# ──────────────────────────────────────────────────────────────
EXPOSE ${MCP_PORT}

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${MCP_PORT}/ || exit 1

CMD ["python", "/app/agent_browser_mcp_server.py"]
