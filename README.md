# 🚀 agent-browser MCP Server

> A **Model Context Protocol (MCP) server** that wraps [agent-browser](https://www.npmjs.com/package/agent-browser) CLI, exposing **70+ browser automation tools** over the **Streamable-HTTP transport**. Unlike traditional MCP servers that run as local subprocesses (stdio), this server runs as an independent **network-accessible service** — supporting multiple concurrent AI agents (Open WebUI, n8n, Claude Desktop, etc.) with persistent, isolated headless Chrome sessions for web automation, scraping, testing, and more.

---

## 🎥 Video Demo

Watch a step-by-step walkthrough of setting up and using the agent-browser MCP server:

<div align="center">
  <a href="https://www.youtube.com/watch?v=MK0FMbtrtn0">
    <img src="https://img.youtube.com/vi/MK0FMbtrtn0/maxresdefault.jpg" alt="agent-browser MCP Server Demo" width="640">
  </a>
  <br>
  <a href="https://www.youtube.com/watch?v=MK0FMbtrtn0">
    📺 Watch on YouTube
  </a>
</div>

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.4.2-green.svg)](https://gofastmcp.com)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)
[![Docker Pulls](https://img.shields.io/docker/pulls/bhdresh/agent-browser-mcp.svg)](https://hub.docker.com/r/bhdresh/agent-browser-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 📋 Table of Contents

- [Video Demo](#-video-demo)
- [Features](#-features)
- [Why Streamable-HTTP?](#-why-streamable-http-over-stdio)
- [Architecture](#-architecture)
- [Quick Start](#-quick-start)
  - [Option A: Pull from Docker Hub (Recommended)](#option-a-pull-from-docker-hub-recommended)
  - [Option B: Build from Source](#option-b-build-from-source)
- [Session Management](#-session-management)
- [Bearer Authentication](#-bearer-authentication)
- [Client Configuration](#-client-configuration)
- [Tool Reference](#-tool-reference)
- [Example Workflows](#-example-workflows)
- [Environment Variables](#-environment-variables)
- [Security Considerations](#-security-considerations)
- [Debugging](#-debugging)
- [Troubleshooting](#-troubleshooting)
- [Development](#-development)
- [Project Structure](#-project-structure)
- [License](#-license)
- [Acknowledgments](#-acknowledgments)

---

## ✨ Features

- **70+ MCP tools** — Full browser automation: navigate, click, fill, snapshot, screenshot, evaluate JavaScript, manage tabs, handle cookies, block network requests, and more
- **Streamable-HTTP transport** — Remote network-accessible server (not a local subprocess). Multiple clients connect simultaneously over a single HTTP endpoint
- **Persistent browser sessions** — Stateful Chrome instances per client. Multi-step workflows (navigate → fill → click → capture) share browser state across tool calls
- **Hybrid session isolation** — Works with **any MCP client** — from stateful (Open WebUI) to stateless (n8n), each gets their own isolated Chrome
- **Headless Chrome** — Ships with Chrome for Testing, no external browser needed
- **Docker-native** — Single container, zero host dependencies beyond Docker
- **Bearer token authentication** — Production-ready auth (not just env vars like stdio servers)
- **Batch execution** — Run multiple commands in a single request for efficiency
- **Universal escape hatch** — `browse_run()` supports any agent-browser CLI subcommand
- **No command injection** — `subprocess.run(list, shell=False)` + `shlex.split()` across all input paths
- **Debug logging** — Built-in request/response tracing when `DEBUG=true`

---

## 🆚 Why Streamable-HTTP Over Stdio?

Most MCP servers run as a **local subprocess** that the client launches via stdin/stdout pipes (stdio transport). This server uses **Streamable-HTTP** — a network-accessible HTTP service. Here's why:

### Traditional Stdio MCP vs. This Server

```
┌─ Traditional Stdio MCP ─────────────────────────────────────┐
│  Client launches server as subprocess                        │
│  ┌──────────┐  spawns  ┌──────────────┐                     │
│  │ AI Agent │ ────────►│ MCP Server   │                     │
│  └──────────┘  stdio   │ (local)      │                     │
│                        └──────────────┘                     │
│  • 1 client = 1 process                                     │
│  • Local machine only                                       │
│  • No authentication                                        │
│  • Stateless — no memory between calls                       │
└─────────────────────────────────────────────────────────────┘

┌─ This Server (Streamable-HTTP) ─────────────────────────────┐
│  Server runs as independent Docker service                   │
│  ┌──────────┐  POST /mcp   ┌───────────────────────────┐    │
│  │ Client A  │ ───────────►│                           │    │
│  └──────────┘              │  MCP Server (Python)      │    │
│  ┌──────────┐  POST /mcp   │                           │    │
│  │ Client B  │ ───────────►│  ┌─────────────────────┐  │    │
│  └──────────┘              │  │ Docker + Headless   │  │    │
│  ┌──────────┐  POST /mcp   │  │ Chrome              │  │    │
│  │ Client C  │ ───────────►│  └─────────────────────┘  │    │
│  └──────────┘              │                           │    │
│                            └───────────────────────────┘    │
│  • Many clients = 1 server                                  │
│  • Network-accessible                                       │
│  • Bearer token auth                                        │
│  • Stateful — persistent Chrome sessions per user            │
└─────────────────────────────────────────────────────────────┘
```

| Dimension | Stdio MCP (Traditional) | Streamable-HTTP (This Server) |
|---|---|---|
| **Transport** | stdin/stdout pipes | HTTP POST → JSON or SSE stream |
| **Deployment** | Client spawns subprocess | Independent Docker service |
| **Concurrent clients** | 1 per process | Many per server ✅ |
| **Session state** | Lost per call | Persistent Chrome sessions ✅ |
| **Authentication** | None (env vars only) | Bearer token ✅ |
| **Network scope** | Local machine only | Remote / multi-user ✅ |
| **Reverse proxy** | N/A | nginx / TLS termination ✅ |
| **Monitoring** | Per-host logs | Centralized ✅ |
| **Browser automation** | ❌ Poor fit (no state) | ✅ Built for it |

### Why Browser Automation REQUIRES Streamable-HTTP

Browser automation is fundamentally **stateful**. A sequence of tool calls must share the same browser state:

```
browse_init()        → Chrome starts (persists across calls)
browse_open(url)     → Page loads
browse_fill(sel, text) → Types into THAT page
browse_click(sel)   → Clicks on THAT page
browse_snapshot()   → Reads from THAT page
browse_cookies_get() → Gets cookies from THAT session
browse_shutdown()   → Chrome closes
```

With stdio, each tool call would need its own fresh Chrome instance — making multi-step workflows impossible. Streamable-HTTP keeps a persistent Chrome process alive, mapped to each client's session.

### How Streamable-HTTP Works

Unlike the **deprecated SSE transport** (two separate endpoints: `POST /messages` + `GET /sse`), Streamable-HTTP uses a **single endpoint**:

```
Client sends → POST /mcp
              → Accept: application/json, text/event-stream

Server responds with ONE of:
  1. Fast response  → 200 + application/json (inline JSON body)
  2. Slow response  → 200 + text/event-stream (SSE events over same connection)
```

The server decides per-request whether to respond instantly or stream. This is cleaner than the old SSE pattern and is the current MCP standard.

---

## 🏗 Architecture

```
┌─────────────────┐    HTTP/SSE    ┌──────────────────────┐    subprocess    ┌──────────────────┐
│  AI Agent       │ ──────────────▶│  MCP Server          │ ──────────────▶  │  Headless        │
│  (Open WebUI)   │   streamable   │  (Python / FastMCP)  │                  │  Chrome          │
│  (n8n)          │ ──────────────▶│  Port :8010           │                  │  (in container)  │
│  (Claude, etc.) │                │                       │                  │                  │
└─────────────────┘                └──────────────────────┘                  └──────────────────┘
```

### Session Flow

```
Client Request
     │
     ▼
┌──────────────────────────────┐
│  _ensure_browser_session()   │
│                              │
│  Strategy 1: FastMCP state   │ ← Open WebUI (stable sessions)
│  Strategy 2: X-Browser-      │ ← n8n with custom header
│           Session header     │
│  Strategy 3: Single shared   │ ← Fallback for stateless clients
└──────────┬───────────────────┘
           │
           ▼
    agent-browser CLI
    (executes command)
           │
           ▼
    Headless Chrome
           │
           ▼
    Response → Client
```

---

## 🚀 Quick Start

### Prerequisites

- Docker 24+ installed
- 2GB+ free disk space (image size: ~1.4GB)

### Option A: Pull from Docker Hub (Recommended)

The easiest way to get started — no build step needed.

```bash
# Pull the pre-built image from Docker Hub
docker pull bhdresh/agent-browser-mcp:latest

# Run the container (add BEARER_TOKEN to enable authentication)
docker run -d \
  --name agent-browser-mcp \
  --restart unless-stopped \
  -p 8010:8010 \
  -v /tmp/:/tmp/ \
  -v agent-browser-data:/root/.agent-browser \
  -e AGENT_BROWSER_ENGINE=chrome \
  -e AGENT_BROWSER_IGNORE_HTTPS_ERRORS=true \
  -e AGENT_BROWSER_SCREENSHOT_DIR=/tmp/ \
  -e MCP_PORT=8010 \
  -e MCP_HOST=0.0.0.0 \
  -e DEBUG=true \
  -e BEARER_TOKEN=your-secret-token \
  bhdresh/agent-browser-mcp:latest
```

### Option B: Build from Source

If you want to customize the image or build from the latest commit:

```bash
# Clone this repository
git clone https://github.com/bhdresh/agent-browser-mcp.git
cd agent-browser-mcp

# Build the Docker image
docker build -t agent-browser-mcp:latest .

# Run the container
docker run -d \
  --name agent-browser-mcp \
  --restart unless-stopped \
  -p 8010:8010 \
  -v /tmp/:/tmp/ \
  -v agent-browser-data:/root/.agent-browser \
  -e AGENT_BROWSER_ENGINE=chrome \
  -e AGENT_BROWSER_IGNORE_HTTPS_ERRORS=true \
  -e AGENT_BROWSER_SCREENSHOT_DIR=/tmp/ \
  -e MCP_PORT=8010 \
  -e MCP_HOST=0.0.0.0 \
  -e DEBUG=true \
  -e BEARER_TOKEN=your-secret-token \
  agent-browser-mcp:latest
```

### Verify

```bash
# Check container is running
docker ps | grep agent-browser-mcp

# Check logs
docker logs agent-browser-mcp

# Test endpoint
curl -s http://localhost:8010/mcp
```

---

## 🔐 Session Management

This server uses a **3-tier hybrid session strategy** that automatically adapts to your MCP client's capabilities:

### Strategy Comparison

| Strategy | Mechanism | Isolation | Clients |
|---|---|---|---|
| **1. FastMCP Native** | `ctx.set_state()` keyed by `ctx.session_id` | ✅ Per-user | Open WebUI, Claude Desktop |
| **2. Custom Header** | `X-Browser-Session` HTTP header | ✅ Per-workflow | n8n, custom clients |
| **3. Shared Fallback** | Global Python singleton | ❌ Shared | Any stateless client |

### How It Works

**For clients with stable MCP sessions (Open WebUI, Claude Desktop):**
- FastMCP assigns a stable `ctx.session_id` per connection
- `ctx.set_state()` / `ctx.get_state()` persists across all tool calls
- Each user/chat gets their own isolated Chrome instance
- **No configuration needed** — works out of the box

**For clients with random MCP sessions per call (n8n):**
- n8n generates a new `mcp-session-id` on every tool call
- FastMCP treats each call as a new client → state is lost
- **Solution:** Add custom header `X-Browser-Session: <name>` to your n8n MCP Client node
- Each unique header value gets its own isolated Chrome instance

**Fallback:**
- If neither strategy 1 nor 2 applies, all calls share a single Chrome instance
- Works but no isolation between concurrent clients

### Visual Decision Flow

```
                    Incoming Tool Call
                         │
                         ▼
            ┌──────────────────────────┐
            │ Strategy 1 Check         │
            │ ctx.get_state()          │
            └──────────┬───────────────┘
                       │
                   Found? ──Yes──→ Return cached session ✅
                       │
                       No
                       │
                       ▼
            ┌──────────────────────────┐
            │ Strategy 2 Check         │
            │ X-Browser-Session header │
            └──────────┬───────────────┘
                       │
                   Set? ──Yes──→ Return mapped session ✅
                       │
                       No
                       │
                       ▼
            ┌──────────────────────────┐
            │ Strategy 3 Fallback      │
            │ Single shared session    │
            └──────────────────────────┘
```

---

## 🔐 Bearer Authentication

This server supports **Bearer token authentication** controlled by the `BEARER_TOKEN` environment variable.

### Authentication Flow

```
                    Incoming Request
                         │
                         ▼
            ┌──────────────────────────┐
            │ Is X-Browser-Session     │
            │ header present?          │
            └──────────┬───────────────┘
                       │
                   Yes ──→ ✅ Authenticated (no further checks)
                       │
                       No
                       │
                       ▼
            ┌──────────────────────────┐
            │ Is BEARER_TOKEN          │
            │ env var set?             │
            └──────────┬───────────────┘
                       │
                   Not set ──→ ✅ No auth required (open access)
                       │
                     Set
                       │
                       ▼
            ┌──────────────────────────┐
            │ Is "Authorization:       │
            │ Bearer <token>" header   │
            │ present and valid?       │
            └──────────┬───────────────┘
                       │
                   Yes ──→ ✅ Authenticated
                       │
                      No
                       │
                       ▼
               ❌ 401 Unauthorized
```

### When BEARER_TOKEN is **not set** (default)

The server operates with **no authentication**. Any client can connect.
The `X-Browser-Session` header still works for session routing (n8n support).

### When BEARER_TOKEN is **set**

Every request must include **one of** the following:

| Header | Purpose |
|---|---|
| `X-Browser-Session: <any-value>` | Acts as authentication. Any non-empty value grants access. Also used for session routing. |
| `Authorization: Bearer <token>` | The `<token>` must exactly match the `BEARER_TOKEN` env var. |

If neither header is provided, the request is rejected with a 401 error.

### Example: Docker with Bearer auth

```bash
docker run -d \
  --name agent-browser-mcp \
  --restart unless-stopped \
  -p 8010:8010 \
  -e BEARER_TOKEN=my-super-secret-token \
  -e MCP_PORT=8010 \
  -e MCP_HOST=0.0.0.0 \
  bhdresh/agent-browser-mcp:latest
```

### Example: Testing with curl

**With X-Browser-Session header (acts as auth):**
```bash
curl -H "X-Browser-Session: my-session" http://localhost:8010/mcp
```

**With Bearer token:**
```bash
curl -H "Authorization: Bearer my-super-secret-token" http://localhost:8010/mcp
```

**Without auth (rejected when BEARER_TOKEN is set):**
```bash
curl http://localhost:8010/mcp
# → 401 Unauthorized
```

---

## 🔌 Client Configuration

### Open WebUI

**No configuration needed.** Open WebUI maintains stable MCP sessions natively. The server automatically uses Strategy 1 (FastMCP native state) for per-user isolation.

**Setup in Open WebUI:**
1. Go to **Settings → Tools → MCP Servers**
2. Add a new MCP server:
   - **Name:** `agent-browser`
   - **URL:** `http://<host-ip>:8010/mcp`
   - **Transport:** `streamable-http`
3. Save and start a new chat

### n8n

n8n's MCP Client node generates a random `mcp-session-id` per tool call. To maintain session consistency, add a custom header:

**Setup in n8n MCP Client node:**
1. Configure the MCP Client node:
   - **Server URL:** `http://<host-ip>:8010/mcp`
   - **Transport:** `streamable-http`
2. Add a **custom header** in the node's header configuration:

| Header Name | Header Value |
|---|---|
| `X-Browser-Session` | `default` |

3. **For multiple workflows with isolation**, use different values:

| Workflow | X-Browser-Session Value |
|---|---|
| Vulnerability Scanning | `scan-workflow` |
| Report Generation | `report-workflow` |
| General Browsing | `default` |

### Claude Desktop

Works out of the box with Strategy 1. Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-browser": {
      "url": "http://localhost:8010/mcp",
      "transport": "streamable-http"
    }
  }
}
```

### Generic MCP Client

If your client:
- **Maintains stable sessions** → Strategy 1 works automatically
- **Generates random session IDs** → Add `X-Browser-Session` header for Strategy 2
- **Can't set custom headers** → Falls through to Strategy 3 (shared session)

---

## 🛠 Tool Reference

All tools are prefixed with `browse_`. Here's a categorized summary:

### Session Lifecycle

| Tool | Description |
|---|---|
| `browse_init()` | Initialize browser session (call first) |
| `browse_whoami()` | Show current session info |
| `browse_shutdown()` | Close browser and clean up |
| `browse_list_sessions()` | List all active sessions |

### Navigation

| Tool | Description |
|---|---|
| `browse_open(url)` | Navigate to a URL |
| `browse_goback()` | Go back in history |
| `browse_goforward()` | Go forward in history |
| `browse_reload()` | Reload current page |
| `browse_get_url()` | Get current URL |
| `browse_get_title()` | Get page title |

### Reading Content

| Tool | Description |
|---|---|
| `browse_snapshot()` | **Primary** — accessibility tree snapshot |
| `browse_get_text(selector)` | Get text of an element |
| `browse_get_html(selector)` | Get innerHTML of an element |
| `browse_get_value(selector)` | Get input field value |

### Interaction

| Tool | Description |
|---|---|
| `browse_click(selector)` | Click an element |
| `browse_fill(selector, text)` | Clear and fill input |
| `browse_type(selector, text)` | Type without clearing |
| `browse_press(key)` | Press keyboard key |
| `browse_hover(selector)` | Hover over element |
| `browse_select(selector, value)` | Select dropdown option |
| `browse_check(selector)` | Check a checkbox |
| `browse_uncheck(selector)` | Uncheck a checkbox |
| `browse_dblclick(selector)` | Double-click element |
| `browse_focus(selector)` | Focus an element |
| `browse_scrollintoview(selector)` | Scroll element into view |
| `browse_scroll(direction, pixels)` | Scroll the page |
| `browse_upload(selector, file_path)` | Upload a file |

### Visual

| Tool | Description |
|---|---|
| `browse_screenshot()` | Take a screenshot |
| `browse_pdf()` | Save page as PDF |

### Element State Checks

| Tool | Description |
|---|---|
| `browse_is_visible(selector)` | Check if element is visible |
| `browse_is_enabled(selector)` | Check if element is enabled |
| `browse_is_checked(selector)` | Check if checkbox is checked |

### Wait & Timing

| Tool | Description |
|---|---|
| `browse_wait_load(state)` | Wait for page load state |
| `browse_wait_target(selector)` | Wait for element visibility |
| `browse_wait_text(text)` | Wait for text to appear |
| `browse_wait_milliseconds(ms)` | Wait for fixed duration |

### JavaScript & Evaluation

| Tool | Description |
|---|---|
| `browse_eval(js_code)` | Execute JavaScript |

### Tabs

| Tool | Description |
|---|---|
| `browse_tab_list()` | List open tabs |
| `browse_tab_new(url)` | Open new tab |
| `browse_tab_switch(tab_id)` | Switch tabs |
| `browse_tab_close(tab_id)` | Close tab |

### Network

| Tool | Description |
|---|---|
| `browse_network_requests()` | View network requests |
| `browse_network_block(pattern)` | Block URL pattern |
| `browse_network_unblock(pattern)` | Unblock pattern |

### Cookies & Storage

| Tool | Description |
|---|---|
| `browse_cookies_get()` | Get cookies |
| `browse_cookies_set(name, value)` | Set cookie |
| `browse_cookies_clear()` | Clear cookies |
| `browse_storage_local_get(key)` | Get localStorage |
| `browse_storage_local_set(key, value)` | Set localStorage |

### Console & Debug

| Tool | Description |
|---|---|
| `browse_console()` | View console messages |
| `browse_errors()` | View JS errors |

### Viewport

| Tool | Description |
|---|---|
| `browse_viewport_set(width, height, scale)` | Set viewport size |

### Clipboard

| Tool | Description |
|---|---|
| `browse_clipboard_read()` | Read from clipboard |
| `browse_clipboard_write(text)` | Write to clipboard |

### Dialog Handling

| Tool | Description |
|---|---|
| `browse_dialog_accept(text)` | Accept a JS dialog |
| `browse_dialog_dismiss()` | Dismiss a JS dialog |
| `browse_dialog_status()` | Check for open dialogs |

### Semantic Find & Act

| Tool | Description |
|---|---|
| `browse_find_and_click(type, value)` | Semantic find & click |
| `browse_find_and_fill(type, value, text)` | Semantic find & fill |

### Advanced

| Tool | Description |
|---|---|
| `browse_batch(commands)` | Execute multiple commands at once |
| `browse_run(command)` | **Universal** — run any agent-browser CLI command |

### Universal Escape Hatch

When you need something not covered by the named tools, use `browse_run()`:

```
browse_run(command='mouse move 100 200')
browse_run(command='set device "iPhone 14"')
browse_run(command='network har start')
browse_run(command='--help')    # Lists all available commands
```

---

## 🧪 Example Workflows

### Basic: Read a webpage

```
1. browse_init()
2. browse_open("https://example.com")
3. browse_snapshot()           ← Read page content
4. browse_shutdown()
```

### Advanced: Navigate and extract

```
1. browse_init()
2. browse_open("https://example.com/products")
3. browse_snapshot(interactive_only=True)  ← Get clickable elements
4. browse_click("@e5")                     ← Click "View Details"
5. browse_wait_load(state="networkidle")
6. browse_snapshot()                       ← Read product page
7. browse_screenshot()                     ← Capture visual proof
8. browse_get_url()                        ← Confirm URL
9. browse_shutdown()
```

### Batch: Efficient multi-step

```
browse_batch(
    commands='[
        ["open", "https://example.com"],
        ["snapshot", "-i"],
        ["screenshot"]
    ]'
)
```

### Debug: Diagnose a page

```
1. browse_open("https://problem-site.com")
2. browse_eval("document.title + ' | ' + document.URL")
3. browse_console()          ← Check for JS errors
4. browse_errors()           ← Check for uncaught exceptions
5. browse_network_requests() ← Check network activity
6. browse_screenshot()       ← See what the browser actually sees
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8010` | Port the MCP server listens on |
| `MCP_HOST` | `0.0.0.0` | Host interface to bind to |
| `CLI_TIMEOUT` | `60` | Timeout in seconds for agent-browser CLI commands |
| `DEBUG` | `false` | Enable detailed debug logging (`true`/`false`) |
| `AGENT_BROWSER_ENGINE` | `chrome` | Browser engine (`chrome`) |
| `AGENT_BROWSER_IGNORE_HTTPS_ERRORS` | `true` | Skip HTTPS certificate validation |
| `AGENT_BROWSER_SCREENSHOT_DIR` | `/tmp/` | Directory for saved screenshots |
| `AGENT_BROWSER_HEADLESS` | `true` | Run Chrome in headless mode |
| `DOCKER_CONTAINER_NAME` | `""` | Set if running agent-browser in a separate container |
| `BEARER_TOKEN` | `""` | Bearer token for authentication. Leave empty to disable auth. When set, clients must provide either `X-Browser-Session` or `Authorization: Bearer <token>` header. |

---

## 🔒 Security Considerations

- **Port exposure:** Only expose port 8010 to trusted networks. Use the `BEARER_TOKEN` environment variable to enable Bearer token authentication.
- **JS execution:** `browse_eval()` runs arbitrary JavaScript in the context of the loaded page. Be cautious with untrusted URLs.
- **Volume mounts:** `/tmp/` is shared for screenshots. Restrict access if running in multi-tenant environments.
- **Session isolation:** Use `X-Browser-Session` header with unique values per workflow/user to prevent cross-contamination.
- **Network:** The container has full internet access. Consider firewall rules or `browse_network_block()` to restrict outbound traffic.
- **Input sanitization:** All tool inputs are passed via `subprocess.run(list, shell=False)` — no shell injection is possible. The `browse_run()` tool uses `shlex.split()` for safe argument parsing.

### Recommended Production Setup

```bash
# Run behind a reverse proxy with authentication
docker run -d \
  --name agent-browser-mcp \
  --network internal-network \
  -p 127.0.0.1:8010:8010 \
  -e BEARER_TOKEN=your-secret-token \
  bhdresh/agent-browser-mcp:latest
```

---

## 🐛 Debugging

### Enable Debug Logs

Set `DEBUG=true` when running the container:

```bash
docker run -d -e DEBUG=true bhdresh/agent-browser-mcp:latest
```

### View Logs

```bash
# Live tail
docker logs -f agent-browser-mcp

# Recent logs
docker logs --tail 100 agent-browser-mcp
```

### What Debug Logs Show

```
[DEBUG 20:30:01] _ensure_browser_session called | ctx.session_id=abc123
[DEBUG 20:30:01] HTTP headers: {"host":"...", "mcp-session-id":"abc123", ...}
[DEBUG 20:30:01]   → Strategy 1 (FastMCP native): session=browser-1
[DEBUG 20:30:01] browse_open → session=browser-1 url=https://example.com
[DEBUG 20:30:01] CMD  → agent-browser --session browser-1 open https://example.com
[DEBUG 20:30:13] OUT  → rc=0 elapsed=11.90s
[DEBUG 20:30:13] STDOUT → ✓ Example Domain |   https://example.com/
```

Each log entry shows:
- **Timestamp** — When the event occurred
- **Strategy used** — Which session resolution path was taken
- **Full CLI command** — The exact agent-browser command executed
- **Exit code & timing** — Return code and elapsed time
- **Output preview** — First 1000 chars of stdout/stderr

### Diagnose Session Issues

1. Check which strategy is being used:
   ```
   docker logs agent-browser-mcp | grep "Strategy"
   ```

2. Check if `X-Browser-Session` header is being received (for n8n):
   ```
   docker logs agent-browser-mcp | grep "X-Browser-Session"
   ```

3. Check HTTP headers on each request:
   ```
   docker logs agent-browser-mcp | grep "HTTP headers"
   ```

4. Verify session consistency:
   ```
   docker logs agent-browser-mcp | grep "session="
   ```

---

## 🔧 Troubleshooting

### "Empty page" from browse_snapshot()

**Symptom:** `browse_open()` succeeds but `browse_snapshot()` returns `(empty page)`

**Cause:** Each tool call is hitting a **different browser session**

**Fix:**
- **n8n:** Add `X-Browser-Session` custom header
- Verify in logs: all tool calls should show the **same session name**

### n8n tool calls failing

**Symptom:** Tools return errors or empty results

**Steps:**
1. Check debug logs: `docker logs agent-browser-mcp | tail -50`
2. Verify the `mcp-session-id` header — if it changes per call, you need the custom header
3. Check that your n8n MCP Client node points to the correct URL (`http://<host-ip>:8010/mcp`)

### Container won't start

```bash
# Check for port conflicts
docker ps -a | grep 8010

# Check Docker daemon
docker info

# Rebuild from scratch (from Docker Hub)
docker stop agent-browser-mcp && docker rm agent-browser-mcp
docker pull bhdresh/agent-browser-mcp:latest
docker run -d --name agent-browser-mcp -p 8010:8010 bhdresh/agent-browser-mcp:latest
```

### Cloudflare / JavaScript challenge pages

Some sites (Google, certain government sites) detect headless browsers. Workarounds:
- Use `browse_run(command='set device "iPhone 14"')` to emulate a real device
- Use `browse_wait_load(state='networkidle')` for SPAs
- Try `browse_wait_milliseconds(ms=5000)` after opening dynamic pages

---

## 🛠 Development

### Modify the Server

```bash
# Edit the server code
nano agent_browser_mcp_server.py

# Rebuild
docker build -t agent-browser-mcp:latest .

# Restart
docker stop agent-browser-mcp && docker rm agent-browser-mcp
docker run -d --name agent-browser-mcp -p 8010:8010 -e DEBUG=true agent-browser-mcp:latest
```

### Push a New Version to Docker Hub

```bash
# Tag the new image
docker tag agent-browser-mcp:latest bhdresh/agent-browser-mcp:latest

# Push to Docker Hub
docker push bhdresh/agent-browser-mcp:latest
```

### Add a New Tool

Tools are defined as async functions decorated with `@mcp.tool()`:

```python
@mcp.tool()
async def browse_my_new_tool(param1: str, ctx: Context) -> str:
    """Tool description for the AI agent."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["my-command", param1], session=session)
```

### Test Locally

```bash
# Install dependencies
pip install fastmcp

# Run directly (requires agent-browser CLI installed)
DEBUG=true python agent_browser_mcp_server.py
```

---

## 📁 Project Structure

```
agent-browser-mcp/
├── Dockerfile                      # Docker build instructions
├── .dockerignore                   # Files to exclude from Docker build context
├── .gitignore                      # Git ignore rules
├── requirements.txt                # Python dependencies (fastmcp)
├── agent_browser_mcp_server.py     # MCP server application
├── README.md                       # This file
└── LICENSE                         # MIT License
```

---

## 📄 License

MIT License — See [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- [agent-browser](https://www.npmjs.com/package/agent-browser) — The CLI tool powering the browser automation
- [FastMCP](https://gofastmcp.com) — The Python MCP framework
- [Model Context Protocol](https://modelcontextprotocol.io) — The standard that makes this all possible
- [Docker Hub](https://hub.docker.com/r/bhdresh/agent-browser-mcp) — Pre-built Docker image
