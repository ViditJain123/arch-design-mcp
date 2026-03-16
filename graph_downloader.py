"""
graph_downloader.py — Download files from SharePoint via Microsoft Graph API.

Uses device code auth (from graph_auth.py) to access files the user has
permission to view. Handles both sharing links and direct SharePoint URLs.
"""

import base64
import logging
import os
from urllib.parse import urlparse

import httpx

from .graph_auth import get_access_token

logger = logging.getLogger("arch-drawing-analyzer.graph_downloader")

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _encode_sharing_url(url: str) -> str:
    """
    Encode a SharePoint sharing URL for the Graph /shares/ endpoint.
    See: https://learn.microsoft.com/en-us/graph/api/shares-get
    """
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")
    # Remove trailing '=' padding and prepend 'u!'
    return "u!" + encoded.rstrip("=")


def _is_sharepoint_url(url: str) -> bool:
    """Check if a URL is a SharePoint/OneDrive URL."""
    host = urlparse(url).hostname or ""
    return "sharepoint.com" in host or "onedrive" in host.lower()


async def download_from_sharepoint(url: str, dest_dir: str) -> str:
    """
    Download a file from SharePoint using Graph API with user auth.

    Works with:
    - Sharing links (https://tenant.sharepoint.com/:b:/s/site/abc123...)
    - Direct file URLs (https://tenant.sharepoint.com/sites/site/path/file.pdf)

    Args:
        url: SharePoint URL to the file.
        dest_dir: Directory to save the file into.

    Returns:
        Absolute path to the downloaded file.

    Raises:
        RuntimeError: If download fails.
    """
    os.makedirs(dest_dir, exist_ok=True)
    token = get_access_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "ArchDrawingAnalyzer/1.0",
    }

    # Resolve the file via the sharing link endpoint
    encoded = _encode_sharing_url(url)
    resolve_url = f"{_GRAPH_BASE}/shares/{encoded}/driveItem"

    logger.info("Resolving SharePoint URL via Graph API")
    logger.debug("  Original URL: %s", url)
    logger.debug("  Encoded share: %s", encoded)
    logger.debug("  Graph URL: %s", resolve_url)

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=300)) as client:
        # Step 1: Resolve sharing link to driveItem metadata
        resp = await client.get(resolve_url, headers=headers)

        if resp.status_code == 401:
            raise RuntimeError(
                "Graph API returned 401 Unauthorized. Token may have expired. "
                "Try clearing the cache and re-authenticating."
            )

        if resp.status_code != 200:
            logger.error("Failed to resolve sharing link: %d %s", resp.status_code, resp.text[:500])
            raise RuntimeError(
                f"Failed to resolve SharePoint URL: {resp.status_code} — {resp.text[:200]}"
            )

        item = resp.json()
        filename = item.get("name", "download.pdf")
        file_size = item.get("size", 0)
        download_url = item.get("@microsoft.graph.downloadUrl")

        logger.info("Resolved file: %s (%.1f MB)", filename, file_size / (1024 * 1024))

        if not download_url:
            # Fallback: get content via /content endpoint
            download_url = f"{resolve_url}/content"
            logger.info("No direct download URL, using /content endpoint")

        # Step 2: Download the actual file
        logger.info("Downloading file...")
        filepath = os.path.join(dest_dir, filename)

        async with client.stream("GET", download_url, headers=headers, follow_redirects=True) as dl_resp:
            if dl_resp.status_code not in (200, 302):
                raise RuntimeError(f"Download failed: {dl_resp.status_code}")

            bytes_written = 0
            with open(filepath, "wb") as f:
                async for chunk in dl_resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
                    bytes_written += len(chunk)

        logger.info("Downloaded %d bytes to %s", bytes_written, filepath)

        # Validate it's a PDF
        with open(filepath, "rb") as f:
            header = f.read(5)
        if not header.startswith(b"%PDF-"):
            logger.error("Downloaded file is not a PDF (header: %r)", header)
            raise ValueError(f"Downloaded file is not a valid PDF (starts with {header!r})")

        return filepath
