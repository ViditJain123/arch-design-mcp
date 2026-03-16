"""
graph_auth.py — Microsoft Graph authentication via Device Code Flow.

Handles token acquisition, caching, and refresh using MSAL.
No client secret required (public client).

Token cache is stored locally at ~/.arch-design-mcp-token-cache.json
so users only authenticate once.
"""

import json
import logging
import os

import msal

logger = logging.getLogger("arch-drawing-analyzer.graph_auth")

_AUTHORITY_BASE = "https://login.microsoftonline.com"
_SCOPES = ["Files.Read.All", "Sites.Read.All", "User.Read"]
_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".arch-design-mcp-token-cache.json")


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
    Get a valid Graph API access token.

    First tries silent acquisition (cached/refreshed token).
    Falls back to device code flow if no cached token exists.

    Returns:
        Access token string.

    Raises:
        RuntimeError: If authentication fails.
    """
    app, cache = _get_app()

    # Try silent first (cached token or refresh)
    accounts = app.get_accounts()
    if accounts:
        logger.info("Found cached account: %s", accounts[0].get("username", "unknown"))
        result = app.acquire_token_silent(_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            logger.info("Token acquired silently (cached/refreshed)")
            return result["access_token"]
        logger.info("Silent acquisition failed, falling back to device code flow")

    # Device code flow
    flow = app.initiate_device_flow(scopes=_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device code flow failed: {flow.get('error_description', 'unknown error')}")

    # This message will appear in stderr (MCP logs)
    logger.warning("=" * 60)
    logger.warning("AUTHENTICATION REQUIRED")
    logger.warning("  %s", flow["message"])
    logger.warning("=" * 60)

    # Also print to stderr directly so it's visible
    import sys
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"AUTHENTICATION REQUIRED", file=sys.stderr)
    print(f"  {flow['message']}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    # This blocks until user completes auth in browser
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "unknown error"))
        raise RuntimeError(f"Authentication failed: {error}")

    _save_cache(cache)
    logger.info("Token acquired via device code flow for: %s",
                result.get("id_token_claims", {}).get("preferred_username", "unknown"))
    return result["access_token"]


def clear_cache() -> str:
    """Remove the cached token. User will need to re-authenticate."""
    if os.path.exists(_CACHE_PATH):
        os.remove(_CACHE_PATH)
        return f"Token cache cleared: {_CACHE_PATH}"
    return "No token cache found."
