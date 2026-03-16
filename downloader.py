"""
downloader.py — Download PDFs from URLs (including SharePoint links).

Uses httpx for async streaming downloads with redirect following.
"""

import logging
import os
import re
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import httpx

logger = logging.getLogger("arch-drawing-analyzer.downloader")


def _force_download_url(url: str) -> str:
    """Append download=1 to SharePoint/OneDrive URLs to force direct download."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if "sharepoint.com" in host or "onedrive" in host.lower():
        qs = parse_qs(parsed.query)
        qs["download"] = ["1"]
        new_query = urlencode(qs, doseq=True)
        result = urlunparse(parsed._replace(query=new_query))
        logger.info("SharePoint URL detected — rewrote for direct download")
        logger.debug("  Original URL:  %s", url)
        logger.debug("  Download URL:  %s", result)
        return result

    logger.debug("Non-SharePoint URL, using as-is: %s", url)
    return url


def _extract_filename(response: httpx.Response, url: str) -> str:
    """Extract filename from Content-Disposition header, URL, or fallback."""
    cd = response.headers.get("content-disposition", "")
    if cd:
        logger.debug("Content-Disposition header: %s", cd)
        match = re.search(r'filename[*]?=["\']?([^"\';]+)', cd)
        if match:
            name = match.group(1).strip()
            if name:
                logger.info("Filename from Content-Disposition: %s", name)
                return name

    path = urlparse(url).path
    basename = os.path.basename(path)
    if basename and "." in basename:
        logger.info("Filename from URL path: %s", basename)
        return basename

    logger.info("No filename detected, using fallback: download.pdf")
    return "download.pdf"


def _validate_pdf(path: str) -> None:
    """Check that the file starts with PDF magic bytes."""
    file_size = os.path.getsize(path)
    with open(path, "rb") as f:
        header = f.read(256)  # Read more to aid debugging

    logger.info("Downloaded file size: %d bytes (%.1f MB)", file_size, file_size / (1024 * 1024))
    logger.info("File header (first 64 bytes): %r", header[:64])

    if not header.startswith(b"%PDF-"):
        # Log what we actually got to help diagnose
        logger.error("NOT A VALID PDF — first 256 bytes: %r", header)
        if b"<html" in header.lower() or b"<!doctype" in header.lower():
            logger.error("File appears to be HTML — likely a SharePoint login/redirect page")
        elif b"<?xml" in header:
            logger.error("File appears to be XML — possibly a SOAP or error response")
        raise ValueError(
            f"Downloaded file is not a valid PDF (starts with {header[:20]!r})"
        )

    logger.info("PDF validation passed — valid PDF header detected")


async def download_pdf(url: str, dest_dir: str) -> str:
    """
    Download a PDF from a URL into dest_dir.

    Args:
        url: The PDF URL (SharePoint links are auto-adjusted for direct download).
        dest_dir: Directory to save the file into.

    Returns:
        Absolute path to the downloaded PDF file.

    Raises:
        httpx.HTTPStatusError: On non-2xx responses.
        ValueError: If the downloaded file is not a valid PDF.
    """
    os.makedirs(dest_dir, exist_ok=True)
    download_url = _force_download_url(url)

    logger.info("=" * 60)
    logger.info("DOWNLOAD START")
    logger.info("  Input URL:    %s", url)
    logger.info("  Download URL: %s", download_url)
    logger.info("  Dest dir:     %s", dest_dir)
    logger.info("=" * 60)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30),
        event_hooks={
            "request": [_log_request],
            "response": [_log_response],
        },
    ) as client:
        async with client.stream(
            "GET",
            download_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ArchDrawingAnalyzer/1.0)"},
        ) as response:
            # Log final response details after all redirects
            logger.info("--- FINAL RESPONSE (after redirects) ---")
            logger.info("  Status:       %d %s", response.status_code, response.reason_phrase)
            logger.info("  Final URL:    %s", str(response.url))
            logger.info("  Content-Type: %s", response.headers.get("content-type", "MISSING"))
            logger.info("  Content-Length: %s", response.headers.get("content-length", "MISSING"))
            logger.info("  Content-Disposition: %s", response.headers.get("content-disposition", "MISSING"))

            # Log all redirect history
            if response.history:
                logger.info("  Redirect chain (%d hops):", len(response.history))
                for i, r in enumerate(response.history):
                    logger.info("    [%d] %d %s -> %s", i, r.status_code, r.url, r.headers.get("location", "N/A"))
            else:
                logger.info("  No redirects")

            response.raise_for_status()

            filename = _extract_filename(response, url)
            filepath = os.path.join(dest_dir, filename)
            logger.info("  Saving to:    %s", filepath)

            bytes_written = 0
            with open(filepath, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
                    bytes_written += len(chunk)

            logger.info("  Bytes written: %d", bytes_written)

    _validate_pdf(filepath)
    logger.info("DOWNLOAD SUCCESS: %s", filepath)
    return filepath


async def _log_request(request: httpx.Request):
    """Event hook — log every outgoing request."""
    logger.debug(">>> HTTP %s %s", request.method, request.url)
    for key in ("authorization", "cookie", "user-agent"):
        val = request.headers.get(key)
        if val:
            # Mask auth tokens
            if key == "authorization":
                val = val[:20] + "..."
            logger.debug(">>>   %s: %s", key, val)


async def _log_response(response: httpx.Response):
    """Event hook — log every response (including intermediate redirects)."""
    logger.debug("<<< %d %s (from %s)", response.status_code, response.reason_phrase, response.url)
    for key in ("content-type", "location", "content-disposition", "www-authenticate"):
        val = response.headers.get(key)
        if val:
            logger.debug("<<<   %s: %s", key, val)