"""
graph_auth.py — Microsoft Graph authentication via Device Code Flow.

Handles token acquisition, caching, and refresh using MSAL.
No client secret required (public client).

Token cache is stored locally at ~/.arch-design-mcp-token-cache.json
so users only authenticate once.
"""

import asyncio
import json
import logging
import os

import msal

logger = logging.getLogger("arch-drawing-analyzer.graph_auth")

_AUTHORITY_BASE = "https://login.microsoftonline.com"
_SCOPES = ["Files.Read.All", "Sites.Read.All", "User.Read"]
_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".arch-design-mcp-token-cache.json")

# Module-level state for pending device code flow (one at a time)
_pending_flow = None
_pending_app = None
_pending_cache = None


def _load_env() -> tuple[str, str]:
    """Load client_id and tenant_id from .env file next to server.py."""
    from dotenv import dotenv_values

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        raise FileNotFoundError(
            f"Missing .env file at {env_path}. "
            f"Copy .env.example to .env and fill in your Azure app credentials."
        )

    config = dotenv_values(env_path)
    client_id = config.get("SP_MCP_CLIENT_ID", "").strip()
    tenant_id = config.get("SP_MCP_TENANT_ID", "").strip()

    if not client_id or client_id == "your-client-id-here":
        raise ValueError("SP_MCP_CLIENT_ID not configured in .env")
    if not tenant_id or tenant_id == "your-tenant-id-here":
        raise ValueError("SP_MCP_TENANT_ID not configured in .env")

    return client_id, tenant_id


def _load_cache() -> msal.SerializableTokenCache:
    """Load token cache from disk."""
    cache = msal.SerializableTokenCache()
    if os.path.exists(_CACHE_PATH):
        with open(_CACHE_PATH, "r") as f:
            cache.deserialize(f.read())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    """Persist token cache to disk."""
    if cache.has_state_changed:
        with open(_CACHE_PATH, "w") as f:
            f.write(cache.serialize())


def _get_app() -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    """Create MSAL public client app with persistent token cache."""
    client_id, tenant_id = _load_env()
    cache = _load_cache()
    authority = f"{_AUTHORITY_BASE}/{tenant_id}"

    app = msal.PublicClientApplication(
        client_id,
        authority=authority,
        token_cache=cache,
    )

    return app, cache


def get_access_token() -> str:
    """
    Get a valid Graph API access token using cached/refreshed token only.

    Does NOT initiate device code flow — that blocks the MCP server.
    If no cached token exists, raises an error telling the user to run
    the interactive auth command first.

    Returns:
        Access token string.

    Raises:
        RuntimeError: If no cached token or refresh fails.
    """
    app, cache = _get_app()

    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError(
            "Not authenticated to SharePoint. "
            "Use the o365_login_start tool to begin sign-in, "
            "then call o365_login_complete after signing in."
        )

    logger.info("Found cached account: %s", accounts[0].get("username", "unknown"))
    result = app.acquire_token_silent(_SCOPES, account=accounts[0])

    if result and "access_token" in result:
        _save_cache(cache)
        logger.info("Token acquired silently (cached/refreshed)")
        return result["access_token"]

    # Silent refresh failed — token expired beyond refresh window
    _save_cache(cache)
    raise RuntimeError(
        "SharePoint token expired and could not be refreshed. "
        "Use the o365_login_start tool to re-authenticate, "
        "then call o365_login_complete after signing in."
    )


def initiate_device_code() -> dict:
    """
    Start a device code flow and return the URL+code immediately.
    Stores flow state in module globals for complete_device_code() to finish.

    If already authenticated (silent token works), returns early with status.

    Returns:
        Dict with status and either auth info or device code details.
    """
    global _pending_flow, _pending_app, _pending_cache

    app, cache = _get_app()

    # Check if already authenticated
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return {
                "status": "already_authenticated",
                "username": accounts[0].get("username", "unknown"),
            }

    # Initiate device code flow
    flow = app.initiate_device_flow(scopes=_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(
            f"Device code flow failed: {flow.get('error_description', 'unknown error')}"
        )

    # Store state for complete_device_code()
    _pending_flow = flow
    _pending_app = app
    _pending_cache = cache

    return {
        "status": "pending",
        "user_code": flow["user_code"],
        "verification_uri": flow.get("verification_uri", ""),
        "verification_uri_complete": flow.get("verification_uri_complete", ""),
        "message": flow.get("message", ""),
        "expires_in": flow.get("expires_in", 900),
    }


async def complete_device_code(timeout_seconds: int = 90) -> dict:
    """
    Poll for device code flow completion. Blocks up to timeout_seconds.
    Must be called after initiate_device_code().

    Args:
        timeout_seconds: Max seconds to wait (default 90, under MCP's ~120s limit).

    Returns:
        Dict with status and auth result.
    """
    global _pending_flow, _pending_app, _pending_cache

    if _pending_flow is None or _pending_app is None:
        raise RuntimeError(
            "No pending device code flow. Call o365_login_start first."
        )

    flow = _pending_flow
    app = _pending_app
    cache = _pending_cache

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(app.acquire_token_by_device_flow, flow),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        # Abort MSAL's internal polling by expiring the flow
        flow["expires_at"] = 0
        _pending_flow = None
        _pending_app = None
        _pending_cache = None
        return {
            "status": "timeout",
            "message": f"Sign-in not completed within {timeout_seconds}s. "
                       "Call o365_login_start to try again.",
        }

    # Clear pending state
    _pending_flow = None
    _pending_app = None
    _pending_cache = None

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "unknown error"))
        return {"status": "error", "message": f"Authentication failed: {error}"}

    _save_cache(cache)
    username = result.get("id_token_claims", {}).get("preferred_username", "unknown")
    return {"status": "authenticated", "username": username}


def interactive_auth() -> str:
    """
    Run the device code flow interactively in a terminal.
    This is meant to be called from the CLI, not from the MCP server.

    Returns:
        The authenticated user's email/username.
    """
    app, cache = _get_app()

    # Check if already authenticated
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            username = accounts[0].get("username", "unknown")
            print(f"Already authenticated as: {username}")
            print(f"Token is valid. No action needed.")
            return username

    # Device code flow
    flow = app.initiate_device_flow(scopes=_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device code flow failed: {flow.get('error_description', 'unknown error')}")

    # Open browser automatically with pre-filled code
    import webbrowser
    complete_uri = flow.get("verification_uri_complete", flow.get("verification_uri", ""))
    if complete_uri:
        print(f"\nOpening browser for sign-in...")
        print(f"If the browser doesn't open, go to: {flow['verification_uri']}")
        print(f"Enter code: {flow['user_code']}\n")
        webbrowser.open(complete_uri)
    else:
        print(f"\n{flow['message']}\n")

    print("Waiting for sign-in to complete...")

    # This blocks until user completes auth in browser
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "unknown error"))
        raise RuntimeError(f"Authentication failed: {error}")

    _save_cache(cache)
    username = result.get("id_token_claims", {}).get("preferred_username", "unknown")
    print(f"\nAuthenticated as: {username}")
    print(f"Token cached at: {_CACHE_PATH}")
    print(f"You can now use SharePoint links in Claude.")
    return username


def clear_cache() -> str:
    """Remove the cached token. User will need to re-authenticate."""
    if os.path.exists(_CACHE_PATH):
        os.remove(_CACHE_PATH)
        return f"Token cache cleared: {_CACHE_PATH}"
    return "No token cache found."


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--clear":
        print(clear_cache())
    else:
        try:
            interactive_auth()
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
