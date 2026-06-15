"""
agent-browser MCP Server
==========================================

A remote MCP server that wraps the agent-browser CLI, exposing all core
browser automation capabilities as MCP tools over streamable-HTTP transport.

Architecture:
  ┌─────────────────┐    HTTP/SSE    ┌──────────────────────┐
  │  AI Agent (Chat A) ─────────────►│  MCP Server           │
  │  AI Agent (Chat B) ─────────────►│  Python / FastMCP      │
  └─────────────────┘                └──────┬───────────────┘
                                           │ subprocess
                                     ┌──────▼──────────┐
                                     │  Docker Container│
                                     │  agent-browser   │
                                     │  + Headless Chrome│
                                     └─────────────────┘

Session Model — FastMCP Native Per-Client Isolation:
  FastMCP's streamable-HTTP gives each client connection a unique session_id.
  Each MCP session (one per AI chat window) gets its own agent-browser session
  via ctx.set_state()/ctx.get_state().

  ┌──────────────────────────────────────────────────────────────┐
  │  Chat A (Alice, Gmail)  │  session_id: abc123  │ browser-abc123  │
  │  Chat B (Bob, Wiki)     │  session_id: def456  │ browser-def456  │
  │  Chat C (Alice resumes) │  session_id: ghi789  │ browser-ghi789  │
  │                          │                      │                │
  │  Each has:              │                      │                │
  │  - Own Chrome process   │                      │                │
  │  - Own cookies/tabs     │                      │                │
  │  - Own page state       │                      │                │
  └──────────────────────────────────────────────────────────────┘

  The AI agent calls browse_init() at the start of browser work to get
  a named session. All subsequent tools use that session automatically
  (tracked in FastMCP's per-session state).

Author: Bhadresh Patel
Based on: stock-docker MCP pattern (FastMCP + streamable-HTTP)
"""

from fastmcp import FastMCP
from fastmcp.server import Context  # type: ignore
from fastmcp.server.dependencies import get_http_request  # For raw mcp-session-id header
from fastmcp.server.middleware import Middleware
from fastmcp.exceptions import AuthorizationError
import subprocess
import json
import os
import tempfile
import uuid
import threading
import time
import shlex
from typing import Optional

# ──────────────────────────────────────────────────────────────
# Debug logging
# ──────────────────────────────────────────────────────────────
DEBUG = os.getenv("DEBUG", "true").lower() in ("true", "1", "yes")

def debug_log(msg: str):
    """Print debug log with timestamp."""
    if DEBUG:
        ts = time.strftime("%H:%M:%S")
        print(f"[DEBUG {ts}] {msg}", flush=True)

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────

DOCKER_CONTAINER_NAME = os.getenv("DOCKER_CONTAINER_NAME", "")
CLI_TIMEOUT = int(os.getenv("CLI_TIMEOUT", "60"))
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "")

# ──────────────────────────────────────────────────────────────
# Authentication Middleware
# ──────────────────────────────────────────────────────────────
# Two-tier auth strategy:
#
#   Tier 1 — X-Browser-Session header present:
#       Acts as authentication. No further validation needed.
#       (Preserves existing n8n / custom-client behavior.)
#
#   Tier 2 — X-Browser-Session header NOT present:
#       Requires "Authorization: Bearer <token>" header.
#       The token must match the BEARER_TOKEN env variable.
#
#   When BEARER_TOKEN is not set (empty), auth is entirely optional
#   (X-Browser-Session still works as session routing, no auth gate).


class BearerAuthMiddleware(Middleware):
    """Middleware that enforces Bearer token auth when X-Browser-Session is absent."""

    async def on_request(
        self,
        context,
        call_next,
    ):
        # When BEARER_TOKEN is not configured, skip auth entirely
        if not BEARER_TOKEN:
            return await call_next(context)

        try:
            request = get_http_request()
        except Exception:
            # Cannot inspect headers (e.g. non-HTTP transport). Skip auth.
            return await call_next(context)

        # ── Tier 1: X-Browser-Session header acts as authentication ──
        x_browser_session = (
            request.headers.get("X-Browser-Session")
            or request.headers.get("x-browser-session")
        )
        if x_browser_session:
            debug_log(f"AUTH → X-Browser-Session present ('{x_browser_session}'): authenticated")
            return await call_next(context)

        # ── Tier 2: Require Bearer token ──
        auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
        if not auth_header:
            debug_log("AUTH → REJECTED: no X-Browser-Session and no Authorization header")
            raise AuthorizationError(
                "Unauthorized. Provide either 'X-Browser-Session' header "
                "or 'Authorization: Bearer <token>' header."
            )

        # Parse "Bearer <token>"
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            debug_log("AUTH → REJECTED: Authorization header not in 'Bearer <token>' format")
            raise AuthorizationError(
                "Unauthorized. Authorization header must be in format: "
                "'Authorization: Bearer <token>'"
            )

        provided_token = parts[1]
        if provided_token != BEARER_TOKEN:
            debug_log("AUTH → REJECTED: Bearer token mismatch")
            raise AuthorizationError(
                "Unauthorized. Invalid Bearer token."
            )

        debug_log("AUTH → Bearer token validated successfully")
        return await call_next(context)


mcp = FastMCP(
    name="AgentBrowserServer",
    middleware=[BearerAuthMiddleware()],
)

# Key used in FastMCP's per-session state store
STATE_KEY_BROWSER_SESSION = "browser_session"


# ──────────────────────────────────────────────────────────────
# Global Session Registry (for listing/cleanup only)
# ──────────────────────────────────────────────────────────────

class GlobalSessionRegistry:
    """
    Lightweight global registry that tracks which agent-browser sessions
    exist across ALL MCP clients. Used ONLY for browse_list_sessions()
    and cleanup — NOT for routing tool calls.

    Per-client session routing is handled by FastMCP's own session_id
    + ctx.set_state() / ctx.get_state().
    """

    def __init__(self):
        self._lock = threading.Lock()
        # browser_session_name → metadata
        self._sessions: dict[str, dict] = {}
        self._counter = 0

    def allocate(self) -> str:
        """Allocate a new unique browser session name."""
        with self._lock:
            self._counter += 1
            session_name = f"browser-{self._counter}"
            self._sessions[session_name] = {
                "browser_session": session_name,
                "created_at": time.time(),
                "status": "active",
            }
            return session_name

    def mark_closed(self, session_name: str):
        with self._lock:
            if session_name in self._sessions:
                self._sessions[session_name]["status"] = "closed"

    def list_all(self) -> list[dict]:
        with self._lock:
            return list(self._sessions.values())


global_registry = GlobalSessionRegistry()


# ──────────────────────────────────────────────────────────────
# Session helpers — Hybrid isolation model
# ──────────────────────────────────────────────────────────────
# Strategy 1: FastMCP ctx.set_state() — for clients with stable sessions
#             (Open WebUI, Claude Desktop, etc.)
# Strategy 2: Custom X-Browser-Session header — for clients with random
#             mcp-session-id per call (n8n MCP Client node)
# Strategy 3: Single shared fallback — emergency last resort

def _get_http_headers(ctx: Context) -> dict:
    """
    Extract all HTTP headers from the current request for debugging.
    Also returns the X-Browser-Session header value if present.

    NOTE: We no longer use mcp-session-id for routing — it was removed
    because n8n generates a random one per tool call, breaking session
    persistence. Use X-Browser-Session header instead for n8n.
    """
    try:
        request = get_http_request()
        all_headers = dict(request.headers)
        debug_log(f"HTTP headers: {json.dumps(all_headers, default=str)}")
        return all_headers
    except Exception as e:
        debug_log(f"  → Failed to get HTTP request: {e}")
        return {}


def _get_custom_session_header(headers: dict) -> Optional[str]:
    """
    Extract the X-Browser-Session header value from the request.

    This is the PRIMARY mechanism for n8n (and similar stateless MCP clients)
    to maintain a stable browser session across tool calls.

    Usage in n8n MCP Client node:
      Add custom header:  X-Browser-Session:  my-workflow

    Multiple n8n workflows can use different values to stay isolated:
      Workflow A: X-Browser-Session: scan-workflow
      Workflow B: X-Browser-Session: report-workflow

    Returns the header value, or None if not present.
    """
    # Check both cases for robustness
    return headers.get("X-Browser-Session") or headers.get("x-browser-session")


# ── Single shared session (Strategy 3 — emergency fallback) ──
_SINGLE_SESSION: Optional[str] = None
_SINGLE_SESSION_LOCK = threading.Lock()


def _get_or_create_single_session() -> str:
    """
    Return a single persistent browser session.
    Used as a last-resort fallback when no other strategy works.
    """
    global _SINGLE_SESSION
    if _SINGLE_SESSION is None:
        with _SINGLE_SESSION_LOCK:
            if _SINGLE_SESSION is None:
                _SINGLE_SESSION = global_registry.allocate()
                _run_cmd_sync(["init"], session=_SINGLE_SESSION, timeout=30)
                debug_log(f"Created single shared fallback session: {_SINGLE_SESSION}")
    return _SINGLE_SESSION


# ── Custom header → browser session mapping (Strategy 2) ──
# Maps X-Browser-Session header values to browser session names.
# Each unique header value gets its own isolated Chrome instance.
_CUSTOM_SESSION_MAP: dict[str, str] = {}
_CUSTOM_SESSION_MAP_LOCK = threading.Lock()


def _get_or_create_custom_session(header_value: str) -> str:
    """
    Map an X-Browser-Session header value to a browser session.
    If this header value has been seen before, return the existing mapping.
    Otherwise, allocate a new browser session and store the mapping.
    """
    with _CUSTOM_SESSION_MAP_LOCK:
        if header_value not in _CUSTOM_SESSION_MAP:
            browser_session = global_registry.allocate()
            _CUSTOM_SESSION_MAP[header_value] = browser_session
            debug_log(f"Custom session mapping: X-Browser-Session='{header_value}' → {browser_session}")
        return _CUSTOM_SESSION_MAP[header_value]


async def _ensure_browser_session(ctx: Context) -> str:
    """
    Resolve the browser session for the current request.

    Hybrid 3-tier strategy:

    Strategy 1 — FastMCP native state (ctx.set_state):
        For clients with stable MCP sessions (Open WebUI, Claude Desktop).
        Each chat/user gets full browser isolation.

    Strategy 2 — Custom X-Browser-Session header:
        For clients with random mcp-session-id per call (n8n).
        The header value acts as a stable session key.
        Multiple n8n workflows with different header values stay isolated.

    Strategy 3 — Single shared fallback:
        Emergency last resort when neither of the above works.

    Args:
        ctx: FastMCP Context (injected automatically by the framework)

    Returns:
        The browser session name for this MCP session.
    """
    debug_log(f"_ensure_browser_session called | ctx.session_id={ctx.session_id}")

    # Fetch headers once (also logs them for debugging)
    headers = _get_http_headers(ctx)

    # ── Strategy 1: FastMCP's built-in state store ──
    session = await ctx.get_state(STATE_KEY_BROWSER_SESSION)
    if session is not None:
        debug_log(f"  → Strategy 1 (FastMCP native): session={session}")
        return session

    # ── Strategy 2: Custom X-Browser-Session header (n8n) ──
    custom_key = _get_custom_session_header(headers)
    if custom_key is not None:
        session = _get_or_create_custom_session(custom_key)
        # Cache in FastMCP state for subsequent calls within same ctx
        await ctx.set_state(STATE_KEY_BROWSER_SESSION, session)
        debug_log(f"  → Strategy 2 (X-Browser-Session='{custom_key}'): session={session}")
        return session

    # ── Strategy 3: Single shared fallback ──
    debug_log(f"  → Strategy 3 (single shared fallback)")
    return _get_or_create_single_session()


async def _get_browser_session(ctx: Context) -> Optional[str]:
    """Get the current browser session or None if not yet initialized."""
    # Try FastMCP state first
    session = await ctx.get_state(STATE_KEY_BROWSER_SESSION)
    if session is not None:
        return session

    # Try custom header
    headers = _get_http_headers(ctx)
    custom_key = _get_custom_session_header(headers)
    if custom_key is not None:
        with _CUSTOM_SESSION_MAP_LOCK:
            if custom_key in _CUSTOM_SESSION_MAP:
                return _CUSTOM_SESSION_MAP[custom_key]

    # Fall back to single session
    return _SINGLE_SESSION


# ──────────────────────────────────────────────────────────────
# Core helper — run an agent-browser CLI command
# ──────────────────────────────────────────────────────────────

def _run_cmd_sync(
    args: list[str],
    timeout: int = CLI_TIMEOUT,
    session: str = "browser-1",
) -> str:
    """
    Execute an agent-browser CLI command (synchronous).

    Args:
        args: Command arguments (not including 'agent-browser' itself).
        timeout: Max seconds to wait.
        session: The agent-browser session name to use.

    Returns:
        Stdout string from the command.
    """
    full_cmd = ["agent-browser", "--session", session] + args

    if DOCKER_CONTAINER_NAME:
        full_cmd = ["docker", "exec", DOCKER_CONTAINER_NAME] + full_cmd

    # ── DEBUG: log the full command being executed ──
    debug_log(f"CMD  → {' '.join(full_cmd)}")

    start = time.time()
    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.time() - start

    stdout = result.stdout
    stderr = result.stderr

    # ── DEBUG: log output ──
    debug_log(f"OUT  → rc={result.returncode} elapsed={elapsed:.2f}s")
    if stdout:
        # Truncate long output for readability (first 1000 chars)
        preview = stdout[:1000].replace("\n", " | ")
        if len(stdout) > 1000:
            preview += f" ... (+{len(stdout)-1000} more chars)"
        debug_log(f"STDOUT → {preview}")
    if stderr:
        debug_log(f"STDERR → {stderr[:500].replace(chr(10), ' | ')}")

    if result.returncode != 0:
        stderr_s = stderr.strip()
        stdout_s = stdout.strip()
        output = stdout_s or stderr_s
        debug_log(f"ERROR → Returning error: {output[:300]}")
        return f"[agent-browser error (exit {result.returncode})]: {output}"

    return stdout


# ──────────────────────────────────────────────────────────────
# MCP Tools — Session Lifecycle
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_init(ctx: Context) -> str:
    """
    Initialize a new isolated browser session for this chat conversation.
    CALL THIS FIRST before any other browse_* tool.

    This gives you a dedicated Chrome instance with its own cookies,
    tabs, and history — completely isolated from other agents and
    other chat sessions.

    Returns:
        JSON with your session name and instructions. Save this session
        name — it is automatically used by all subsequent browse_* tools.

    Example:
        1. browse_init() → {"session": "browser-3", ...}
        2. browse_open("https://example.com")
        3. browse_snapshot()
    """
    debug_log(f"browse_init called | ctx.session_id={ctx.session_id}")

    # ── Strategy 1: FastMCP native state (Open WebUI — already has a session) ──
    existing = await ctx.get_state(STATE_KEY_BROWSER_SESSION)
    if existing is not None:
        debug_log(f"browse_init → Strategy 1: reusing FastMCP session {existing}")
        session_name = existing
    else:
        headers = _get_http_headers(ctx)

        # ── Strategy 2: Custom X-Browser-Session header (n8n) ──
        custom_key = _get_custom_session_header(headers)
        if custom_key is not None:
            session_name = _get_or_create_custom_session(custom_key)
            await ctx.set_state(STATE_KEY_BROWSER_SESSION, session_name)
            debug_log(f"browse_init → Strategy 2 (X-Browser-Session='{custom_key}'): session={session_name}")
        else:
            # ── Strategy 3: Single shared fallback ──
            global _SINGLE_SESSION
            with _SINGLE_SESSION_LOCK:
                if _SINGLE_SESSION is not None:
                    debug_log(f"browse_init → Strategy 3: reusing shared session {_SINGLE_SESSION}")
                else:
                    debug_log(f"browse_init → Strategy 3: allocating new shared session")
                    _SINGLE_SESSION = global_registry.allocate()
                    _run_cmd_sync(["init"], session=_SINGLE_SESSION, timeout=30)
                session_name = _SINGLE_SESSION
            await ctx.set_state(STATE_KEY_BROWSER_SESSION, session_name)

    debug_log(f"browse_init → Returning session: {session_name}")

    return json.dumps({
        "session": session_name,
        "message": (
            f"Browser session '{session_name}' created for this chat. "
            f"All subsequent browse_* tools will automatically use this session."
        ),
        "mcp_session_id": ctx.session_id,
        "next_steps": [
            "browse_open(url='https://example.com')  → Navigate to a page",
            "browse_snapshot()                        → Read the page content",
            "browse_click('@e2')                      → Interact with elements",
            "browse_shutdown()                        → Clean up when done",
        ],
    })


@mcp.tool()
async def browse_whoami(ctx: Context) -> str:
    """
    Show which browser session this chat is currently using.
    Useful to confirm your session identity at any time.

    Returns:
        JSON with your session details.
    """
    session_name = await _get_browser_session(ctx)

    if session_name is None:
        return json.dumps({
            "status": "not_initialized",
            "message": (
                "You do not have an active browser session. "
                "Call browse_init() to create one."
            ),
        })

    return json.dumps({
        "status": "active",
        "browser_session": session_name,
        "mcp_session_id": ctx.session_id,
        "message": (
            f"You are using browser session '{session_name}'. "
            f"This is isolated from all other agents."
        ),
    })


@mcp.tool()
async def browse_shutdown(ctx: Context) -> str:
    """
    Shut down your browser session — close Chrome and clean up.
    Call this when you're done browsing in this chat.

    Returns:
        Confirmation that the browser was closed.
    """
    session_name = await _get_browser_session(ctx)

    if session_name is None:
        return json.dumps({
            "status": "already_closed",
            "message": "No active browser session to shut down.",
        })

    debug_log(f"browse_shutdown → closing session: {session_name}")

    # Close the Chrome instance
    _run_cmd_sync(["close"], session=session_name)

    # Mark as closed in global registry
    global_registry.mark_closed(session_name)

    # Clear from FastMCP state
    await ctx.set_state(STATE_KEY_BROWSER_SESSION, None)

    # If this session came from a custom header mapping, remove it too
    # so a fresh one is created on next browse_init()
    with _CUSTOM_SESSION_MAP_LOCK:
        keys_to_remove = [
            k for k, v in _CUSTOM_SESSION_MAP.items()
            if v == session_name
        ]
        for k in keys_to_remove:
            del _CUSTOM_SESSION_MAP[k]
            debug_log(f"browse_shutdown → removed custom mapping for '{k}'")

    return json.dumps({
        "status": "shutdown",
        "browser_session": session_name,
        "message": f"Browser session '{session_name}' closed. Chrome terminated.",
    })


@mcp.tool()
async def browse_list_sessions(ctx: Context) -> str:
    """
    List all browser sessions known to this MCP server.
    Shows both your session and any other active sessions.

    Returns:
        JSON list of all sessions with status, including custom header mappings.
    """
    my_session = await _get_browser_session(ctx)

    # Get agent-browser's native session list
    try:
        native_sessions = _run_cmd_sync(["session", "list"], session="default")
    except Exception:
        native_sessions = "Could not query native sessions"

    all_managed = global_registry.list_all()

    # Include custom header → session mappings for diagnostics
    with _CUSTOM_SESSION_MAP_LOCK:
        custom_mappings = dict(_CUSTOM_SESSION_MAP)

    return json.dumps({
        "your_session": my_session,
        "managed_sessions": all_managed,
        "custom_header_mappings": custom_mappings,
        "browser_sessions": native_sessions.strip(),
        "total_managed": len(all_managed),
        "active_count": len([s for s in all_managed if s["status"] == "active"]),
    })


# ──────────────────────────────────────────────────────────────
# MCP Tools — Navigation
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_open(url: str, ctx: Context) -> str:
    """
    Open a URL in the headless browser.
    Your session is used automatically.

    Args:
        url: The URL to navigate to (e.g., 'https://example.com')

    Returns:
        Command output confirming navigation.
    """
    session = await _ensure_browser_session(ctx)
    debug_log(f"browse_open → session={session} url={url}")
    result = _run_cmd_sync(["open", url], session=session)
    debug_log(f"browse_open → result: {result[:300]}")
    return result


@mcp.tool()
async def browse_goback(ctx: Context) -> str:
    """Navigate back to the previous page."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["back"], session=session)


@mcp.tool()
async def browse_goforward(ctx: Context) -> str:
    """Navigate forward to the next page."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["forward"], session=session)


@mcp.tool()
async def browse_reload(ctx: Context) -> str:
    """Reload the current page."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["reload"], session=session)


@mcp.tool()
async def browse_get_url(ctx: Context) -> str:
    """Get the current page URL."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["get", "url"], session=session)


@mcp.tool()
async def browse_get_title(ctx: Context) -> str:
    """Get the current page title."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["get", "title"], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Snapshot & Reading
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_snapshot(
    interactive_only: bool = False,
    compact: bool = False,
    max_depth: Optional[int] = None,
    ctx: Context = None,
) -> str:
    """
    Get the accessibility tree snapshot of the current page.
    PRIMARY way to read page content for AI agents.

    Each element has a ref (e.g., @e1, @e2) that you use with
    browse_click, browse_fill, etc. to interact with it.

    Typical workflow:
      1. browse_open("https://example.com")
      2. browse_snapshot(interactive_only=True)   ← Read the page
      3. browse_click("@e2")                       ← Act on what you read
      4. browse_snapshot()                         ← Re-read after action

    Args:
        interactive_only: If True, only return interactive elements (buttons, links, inputs).
        compact: If True, remove empty structural elements.
        max_depth: Limit the tree depth to this number of levels.

    Returns:
        Accessibility tree with element refs for interaction.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["snapshot"]
    if interactive_only:
        args.append("-i")
    if compact:
        args.append("-c")
    if max_depth is not None:
        args.extend(["-d", str(max_depth)])
    debug_log(f"browse_snapshot → session={session} args={args}")
    result = _run_cmd_sync(args, session=session)
    result_preview = result[:500].replace("\n", " | ")
    debug_log(f"browse_snapshot → result length={len(result)} preview: {result_preview}")
    return result


@mcp.tool()
async def browse_get_text(selector: str, ctx: Context) -> str:
    """
    Get the text content of an element.

    Args:
        selector: Element selector — ref from snapshot (e.g., '@e1') or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["get", "text", selector], session=session)


@mcp.tool()
async def browse_get_html(selector: str, ctx: Context) -> str:
    """
    Get the innerHTML of an element.

    Args:
        selector: Element selector — ref from snapshot (e.g., '@e1') or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["get", "html", selector], session=session)


@mcp.tool()
async def browse_get_value(selector: str, ctx: Context) -> str:
    """
    Get the current value of an input element.

    Args:
        selector: Element selector — ref from snapshot or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["get", "value", selector], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Interaction
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_click(selector: str, new_tab: bool = False, ctx: Context = None) -> str:
    """
    Click an element on the page.

    Args:
        selector: Element selector — ref from snapshot (e.g., '@e2') or CSS selector.
        new_tab: If True, open the link in a new tab.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["click", selector]
    if new_tab:
        args.append("--new-tab")
    return _run_cmd_sync(args, session=session)


@mcp.tool()
async def browse_fill(selector: str, text: str, ctx: Context) -> str:
    """
    Clear and fill an input field with text.

    Args:
        selector: Element selector — ref from snapshot or CSS selector.
        text: The text to fill into the field.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["fill", selector, text], session=session)


@mcp.tool()
async def browse_type(selector: str, text: str, ctx: Context) -> str:
    """
    Type text into an element (without clearing existing content).

    Args:
        selector: Element selector — ref from snapshot or CSS selector.
        text: The text to type.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["type", selector, text], session=session)


@mcp.tool()
async def browse_press(key: str, ctx: Context) -> str:
    """
    Press a keyboard key.

    Args:
        key: Key to press (e.g., 'Enter', 'Tab', 'Control+a', 'Escape').
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["press", key], session=session)


@mcp.tool()
async def browse_hover(selector: str, ctx: Context) -> str:
    """
    Hover the mouse over an element.

    Args:
        selector: Element selector — ref from snapshot or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["hover", selector], session=session)


@mcp.tool()
async def browse_select(selector: str, value: str, ctx: Context) -> str:
    """
    Select a dropdown option.

    Args:
        selector: Element selector for the dropdown.
        value: The option value to select.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["select", selector, value], session=session)


@mcp.tool()
async def browse_check(selector: str, ctx: Context) -> str:
    """
    Check a checkbox.

    Args:
        selector: Element selector for the checkbox.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["check", selector], session=session)


@mcp.tool()
async def browse_scroll(direction: str, pixels: int = 300, ctx: Context = None) -> str:
    """
    Scroll the page.

    Args:
        direction: Scroll direction — 'up', 'down', 'left', or 'right'.
        pixels: Number of pixels to scroll (default: 300).
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["scroll", direction, str(pixels)], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Screenshot & Visual
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_screenshot(
    full_page: bool = False,
    annotate: bool = False,
    ctx: Context = None,
) -> str:
    """
    Take a screenshot of the current page.

    Args:
        full_page: If True, capture the full scrollable page.
        annotate: If True, overlay numbered labels on interactive elements.

    Returns:
        Path to the saved screenshot file.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["screenshot"]
    if full_page:
        args.append("--full")
    if annotate:
        args.append("--annotate")
    return _run_cmd_sync(args, session=session)


@mcp.tool()
async def browse_pdf(output_path: Optional[str] = None, ctx: Context = None) -> str:
    """
    Save the current page as a PDF.

    Args:
        output_path: Optional path to save the PDF. Defaults to temp directory.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["pdf"]
    if output_path:
        args.append(output_path)
    else:
        file_id = str(uuid.uuid4())
        args.append(f"/tmp/{file_id}.pdf")
    return _run_cmd_sync(args, session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Wait
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_wait_target(selector: str, timeout_ms: int = 30000, ctx: Context = None) -> str:
    """
    Wait for an element to become visible.

    Args:
        selector: Element selector to wait for.
        timeout_ms: Maximum wait time in milliseconds (default: 30000).
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["wait", selector], timeout=max(CLI_TIMEOUT, timeout_ms // 1000 + 5), session=session)


@mcp.tool()
async def browse_wait_text(text: str, timeout_ms: int = 30000, ctx: Context = None) -> str:
    """
    Wait for specific text to appear on the page.

    Args:
        text: Text to wait for (substring match).
        timeout_ms: Maximum wait time in milliseconds.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["wait", "--text", text], timeout=max(CLI_TIMEOUT, timeout_ms // 1000 + 5), session=session)


@mcp.tool()
async def browse_wait_milliseconds(ms: int, ctx: Context = None) -> str:
    """
    Wait for a fixed duration.

    Args:
        ms: Milliseconds to wait.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["wait", str(ms)], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — JavaScript Evaluation
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_eval(js_code: str, ctx: Context) -> str:
    """
    Execute JavaScript in the context of the current page.

    Args:
        js_code: JavaScript code to evaluate.
    """
    session = await _ensure_browser_session(ctx)
    debug_log(f"browse_eval → session={session} js_code={js_code[:200]}")
    result = _run_cmd_sync(["eval", js_code], session=session)
    debug_log(f"browse_eval → result: {result[:500]}")
    return result


# ──────────────────────────────────────────────────────────────
# MCP Tools — Tabs
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_tab_list(ctx: Context) -> str:
    """List all open tabs with their IDs and labels."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["tab"], session=session)


@mcp.tool()
async def browse_tab_new(
    url: Optional[str] = None,
    label: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """
    Open a new tab.

    Args:
        url: Optional URL to open in the new tab.
        label: Optional memorable label for the tab (e.g., 'docs', 'app').
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["tab", "new"]
    if label:
        args.extend(["--label", label])
    if url:
        args.append(url)
    return _run_cmd_sync(args, session=session)


@mcp.tool()
async def browse_tab_switch(tab_id: str, ctx: Context) -> str:
    """
    Switch to a specific tab.

    Args:
        tab_id: Tab ID (e.g., 't1', 't2') or label.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["tab", tab_id], session=session)


@mcp.tool()
async def browse_tab_close(tab_id: Optional[str] = None, ctx: Context = None) -> str:
    """
    Close a tab.

    Args:
        tab_id: Tab ID to close. If None, closes the active tab.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["tab", "close"]
    if tab_id:
        args.append(tab_id)
    return _run_cmd_sync(args, session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Network
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_network_requests(filter_text: Optional[str] = None, ctx: Context = None) -> str:
    """
    View tracked network requests.

    Args:
        filter_text: Optional text to filter requests by.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["network", "requests"]
    if filter_text:
        args.extend(["--filter", filter_text])
    return _run_cmd_sync(args, session=session)


@mcp.tool()
async def browse_network_block(url_pattern: str, ctx: Context) -> str:
    """
    Block network requests matching a URL pattern.

    Args:
        url_pattern: URL pattern to block (e.g., '*ads*', '*.example.com/tracker').
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["network", "route", url_pattern, "--abort"], session=session)


@mcp.tool()
async def browse_network_unblock(url_pattern: Optional[str] = None, ctx: Context = None) -> str:
    """
    Remove a network request block.

    Args:
        url_pattern: Pattern to unblock. If None, removes all routes.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["network", "unroute"]
    if url_pattern:
        args.append(url_pattern)
    return _run_cmd_sync(args, session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Cookies & Storage
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_cookies_get(ctx: Context) -> str:
    """Get all cookies for the current page."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["cookies"], session=session)


@mcp.tool()
async def browse_cookies_set(name: str, value: str, ctx: Context) -> str:
    """
    Set a cookie.

    Args:
        name: Cookie name.
        value: Cookie value.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["cookies", "set", name, value], session=session)


@mcp.tool()
async def browse_cookies_clear(ctx: Context) -> str:
    """Clear all cookies."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["cookies", "clear"], session=session)


@mcp.tool()
async def browse_storage_local_get(key: Optional[str] = None, ctx: Context = None) -> str:
    """
    Get localStorage data.

    Args:
        key: Optional specific key to retrieve. If None, returns all.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["storage", "local"]
    if key:
        args.append(key)
    return _run_cmd_sync(args, session=session)


@mcp.tool()
async def browse_storage_local_set(key: str, value: str, ctx: Context) -> str:
    """
    Set a localStorage value.

    Args:
        key: Storage key.
        value: Storage value.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["storage", "local", "set", key, value], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Console & Debug
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_console(ctx: Context) -> str:
    """View browser console messages (log, warn, error, info)."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["console"], session=session)


@mcp.tool()
async def browse_errors(ctx: Context) -> str:
    """View page JavaScript errors (uncaught exceptions)."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["errors"], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Batch Execution
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_batch(commands: str, ctx: Context) -> str:
    """
    Execute multiple agent-browser commands in a single batch.
    More efficient than calling individual tools sequentially.

    Args:
        commands: JSON string of commands array.
                  Example: '[["open","https://example.com"],["snapshot","-i"],["screenshot"]]'
    """
    session = await _ensure_browser_session(ctx)
    try:
        cmd_list = json.loads(commands)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json.dumps(cmd_list))
            temp_path = f.name

        full_cmd = ["agent-browser", "--session", session, "batch", "--json"]
        if DOCKER_CONTAINER_NAME:
            with open(temp_path, 'r') as fh:
                input_data = fh.read()
            docker_cmd = ["docker", "exec", DOCKER_CONTAINER_NAME] + full_cmd
            proc = subprocess.run(
                docker_cmd,
                input=input_data,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
            )
        else:
            with open(temp_path, 'r') as fh:
                input_data = fh.read()
            proc = subprocess.run(
                full_cmd,
                input=input_data,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
            )
        os.unlink(temp_path)

        if proc.returncode != 0:
            return f"[agent-browser batch error (exit {proc.returncode})]: {proc.stderr.strip()}"
        return proc.stdout
    except json.JSONDecodeError as e:
        return f"Invalid JSON in commands: {e}"
    except Exception as e:
        return f"Batch execution error: {e}"


# ──────────────────────────────────────────────────────────────
# MCP Tools — Viewport & Settings
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_viewport_set(width: int, height: int, scale: float = 1.0, ctx: Context = None) -> str:
    """
    Set the browser viewport size.

    Args:
        width: Viewport width in pixels.
        height: Viewport height in pixels.
        scale: Device pixel ratio / scale factor (default: 1.0).
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["set", "viewport", str(width), str(height), str(scale)], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Semantic Find & Act
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_find_and_click(find_type: str, find_value: str, action: str = "click", ctx: Context = None) -> str:
    """
    Find an element semantically and perform an action on it.

    Args:
        find_type: Type of search — 'role', 'text', 'label', 'placeholder', 'alt', 'title', 'testid', 'first', 'last'.
        find_value: The value to search for.
        action: Action to perform — 'click', 'fill', 'type', 'hover', 'focus', 'check', 'text'.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["find", find_type, find_value, action], session=session)


@mcp.tool()
async def browse_find_and_fill(find_type: str, find_value: str, fill_text: str, ctx: Context) -> str:
    """
    Find an element semantically and fill it with text.

    Args:
        find_type: Type of search — 'role', 'text', 'label', 'placeholder', 'alt', 'title', 'testid'.
        find_value: The value to search for (e.g., button name, label text).
        fill_text: The text to fill into the found element.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["find", find_type, find_value, "fill", fill_text], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Clipboard
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_clipboard_read(ctx: Context) -> str:
    """Read text from the browser clipboard."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["clipboard", "read"], session=session)


@mcp.tool()
async def browse_clipboard_write(text: str, ctx: Context) -> str:
    """
    Write text to the browser clipboard.

    Args:
        text: Text to write to clipboard.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["clipboard", "write", text], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Dialog Handling
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_dialog_accept(text: Optional[str] = None, ctx: Context = None) -> str:
    """
    Accept a JavaScript dialog (alert, confirm, prompt).

    Args:
        text: Optional text to provide for a prompt dialog.
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    args = ["dialog", "accept"]
    if text:
        args.append(text)
    return _run_cmd_sync(args, session=session)


@mcp.tool()
async def browse_dialog_dismiss(ctx: Context) -> str:
    """Dismiss a JavaScript dialog."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["dialog", "dismiss"], session=session)


@mcp.tool()
async def browse_dialog_status(ctx: Context) -> str:
    """Check if a JavaScript dialog is currently open."""
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["dialog", "status"], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Missing High-Frequency Interactions
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_uncheck(selector: str, ctx: Context) -> str:
    """
    Uncheck a checkbox.

    Args:
        selector: Element selector — ref from snapshot (e.g., '@e2') or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["uncheck", selector], session=session)


@mcp.tool()
async def browse_dblclick(selector: str, ctx: Context) -> str:
    """
    Double-click an element on the page.

    Args:
        selector: Element selector — ref from snapshot (e.g., '@e2') or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["dblclick", selector], session=session)


@mcp.tool()
async def browse_focus(selector: str, ctx: Context) -> str:
    """
    Focus an element on the page.

    Args:
        selector: Element selector — ref from snapshot (e.g., '@e2') or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["focus", selector], session=session)


@mcp.tool()
async def browse_scrollintoview(selector: str, ctx: Context) -> str:
    """
    Scroll a specific element into the viewport.
    Use this before clicking elements that may be off-screen.

    Args:
        selector: Element selector — ref from snapshot (e.g., '@e2') or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["scrollintoview", selector], session=session)


@mcp.tool()
async def browse_upload(selector: str, file_path: str, ctx: Context) -> str:
    """
    Upload a file through a file input element.

    Args:
        selector: Element selector for the file input — ref from snapshot or CSS selector.
        file_path: Absolute path to the file to upload.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["upload", selector, file_path], session=session)


@mcp.tool()
async def browse_wait_load(
    state: str = "networkidle",
    timeout_ms: int = 30000,
    ctx: Context = None,
) -> str:
    """
    Wait for the page to reach a specific load state.
    Critical for SPAs and dynamic pages.

    Args:
        state: Load state to wait for — 'load', 'domcontentloaded', or 'networkidle' (default).
        timeout_ms: Maximum wait time in milliseconds (default: 30000).
    """
    if ctx is None:
        raise ValueError("Context not available")
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(
        ["wait", "--load", state],
        timeout=max(CLI_TIMEOUT, timeout_ms // 1000 + 5),
        session=session,
    )


@mcp.tool()
async def browse_is_visible(selector: str, ctx: Context) -> str:
    """Check if an element is currently visible on the page.
    Args:
        selector: Element selector — ref from snapshot (e.g., '@e2') or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["is", "visible", selector], session=session)


@mcp.tool()
async def browse_is_enabled(selector: str, ctx: Context) -> str:
    """Check if an element is currently enabled (not disabled).
    Args:
        selector: Element selector — ref from snapshot or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["is", "enabled", selector], session=session)


@mcp.tool()
async def browse_is_checked(selector: str, ctx: Context) -> str:
    """Check if a checkbox or radio button is currently checked.
    Args:
        selector: Element selector — ref from snapshot or CSS selector.
    """
    session = await _ensure_browser_session(ctx)
    return _run_cmd_sync(["is", "checked", selector], session=session)


# ──────────────────────────────────────────────────────────────
# MCP Tools — Universal CLI Runner
# ──────────────────────────────────────────────────────────────

@mcp.tool()
async def browse_run(command: str, ctx: Context) -> str:
    """
    Run ANY agent-browser CLI subcommand directly.

    UNIVERSAL ESCAPE HATCH — use the named browse_* tools first for common
    operations, then fall back to browse_run for anything not covered.

    HOW TO USE:
      The `command` argument is exactly what you'd type after 'agent-browser'
      on the command line. All arguments are a single string with proper quoting.

      browse_run(command='snapshot -i -c')   # compact interactive snapshot
      browse_run(command='set device "iPhone 14"')  # quoted values use double quotes

    HOW TO DISCOVER COMMANDS:
      Call browse_run(command='--help') to see the full command reference.
      This dumps every subcommand and its flags so you can find exactly
      what you need without guessing.

    ─────────────────────────────────────────────────────
    CAPABILITIES BY CATEGORY (use browse_run for these):
    ─────────────────────────────────────────────────────

    MOUSE CONTROL (clicking at coordinates, right-click, drag):
      mouse move <x> <y>       Move cursor to pixel coordinates
      mouse down [left|right|middle]   Press button down
      mouse up [left|right|middle]     Release button
      mouse wheel <dy> [dx]    Scroll wheel (e.g. 'mouse wheel 300')
      drag <sel1> <sel2>       Drag element onto another

    KEYBOARD (real keystrokes, key combos):
      keyboard type <text>     Type with real keystrokes (no selector needed)
      keyboard inserttext <text>  Insert text without key events
      keydown <key>            Hold key down (e.g. 'keydown Control')
      keyup <key>              Release key (e.g. 'keyup Control')

    READ ELEMENT INFO:
      get attr <sel> <attr>    Get element attribute (e.g. 'get attr @e1 href')
      get box <sel>            Get bounding box (x, y, width, height)
      get styles <sel>         Get computed CSS styles
      get count <selector>     Count matching elements
      get cdp-url              Get CDP WebSocket URL for DevTools

    WAIT FOR CONDITIONS:
      wait --url "<pattern>"   Wait for URL to match (e.g. 'wait --url "**/dashboard"')
      wait --fn "<js>"         Wait for JS condition (e.g. 'wait --fn "window.ready === true"')
      wait --load networkidle  Wait for network to idle (also use browse_wait_load)
      wait --load domcontentloaded
      wait --load load

    IFrames:
      frame <sel>              Switch context into iframe
      frame main               Switch back to top-level page

    BROWSER SETTINGS:
      set device "<name>"      Emulate device (e.g. 'set device "iPhone 14"')
      set geo <lat> <lon>      Set geolocation
      set headers '{...}'      Set global HTTP headers
      set credentials <user> <pass>  HTTP basic auth
      set media [dark|light]   Emulate color scheme
      set offline [on|off]     Toggle offline mode

    STORAGE:
      storage session              Get all sessionStorage
      storage session <key>        Get specific key
      storage session set <k> <v>  Set value
      storage session clear        Clear all sessionStorage
      storage local clear          Clear all localStorage

    NETWORK ADVANCED:
      network har start            Begin HAR recording
      network har stop [file.har]  Stop and save HAR file
      network request <id>         View full request/response details
      network route <url> --body <data>  Mock a response

    VISUAL DIFF:
      diff snapshot                            Compare current vs last snapshot
      diff snapshot --selector "#main"         Scoped diff
      diff screenshot --baseline before.png    Visual pixel diff
      diff url <url1> <url2>                   Compare two URLs

    PERFORMANCE & DEBUG:
      trace start / trace stop [file]          CDP trace recording
      profiler start / profiler stop [file]    DevTools profiling
      highlight <sel>                          Highlight element on page
      inspect                                  Open Chrome DevTools

    AUTH STATE:
      state save [file]    Save current auth (cookies + storage)
      state load [file]    Load auth state from file
      state list           List saved state files

    NAVIGATION:
      pushstate <url>      SPA client-side navigation

    CLIPBOARD (Ctrl+C / Ctrl+V):
      clipboard copy       Copy current selection
      clipboard paste      Paste from clipboard

    TROUBLESHOOTING:
      console --clear      Clear console messages
      errors --clear       Clear error messages
      --help               Full command reference (call this to explore!)

    Args:
        command: The agent-browser subcommand and arguments as a single string
                 (without the 'agent-browser' prefix). Use shlex-safe quoting:
                 e.g. 'set device "iPhone 14"', 'wait --url "**/dash"'.
                 Call with '--help' to explore all available commands.

    Returns:
        The stdout output from the agent-browser CLI command.
        On error, returns a descriptive error message with the exit code.
    """
    session = await _ensure_browser_session(ctx)

    # Parse the command string into arg list
    import shlex
    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"[browse_run error]: Failed to parse command: {e}"

    return _run_cmd_sync(args, session=session)


# ──────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", "8000"))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    print(f"🚀 agent-browser MCP server starting on {host}:{port}")
    print(f"   Session model: Per-client (FastMCP session_id → agent-browser --session)")
    print(f"   Docker container: {DOCKER_CONTAINER_NAME or '(direct)'}")
    if BEARER_TOKEN:
        print(f"   Auth: Bearer token enabled (X-Browser-Session or Bearer required)")
    else:
        print(f"   Auth: Bearer token disabled (BEARER_TOKEN not set)")
    mcp.run(transport="streamable-http", host=host, port=port)
