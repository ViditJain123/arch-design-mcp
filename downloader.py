"""
downloader.py — Download PDFs from URLs (including SharePoint links).

Uses httpx for async streaming downloads with redirect following.
"""

import os
import re
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import httpx


def _force_download_url(url: str) -> str:
    """Append download=1 to SharePoint/OneDrive URLs to force direct download."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if "sharepoint.com" in host or "onedrive" in host.lower():
        qs = parse_qs(parsed.query)
        qs["download"] = ["1"]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    return url


def _extract_filename(response: httpx.Response, url: str) -> str:
    """Extract filename from Content-Disposition header, URL, or fallback."""
    cd = response.headers.get("content-disposition", "")
    if cd:
        match = re.search(r'filename[*]?=["\']?([^"\';]+)', cd)
        if match:
            name = match.group(1).strip()
            if name:
                return name

    path = urlparse(url).path
    basename = os.path.basename(path)
    if basename and "." in basename:
        return basename

    return "download.pdf"


def _validate_pdf(path: str) -> None:
    """Check that the file starts with PDF magic bytes."""
    with open(path, "rb") as f:
        header = f.read(5)
    if not header.startswith(b"%PDF-"):
        raise ValueError(
            f"Downloaded file is not a valid PDF (starts with {header!r})"
        )


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

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30),
    ) as client:
        async with client.stream(
            "GET",
            download_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ArchDrawingAnalyzer/1.0)"},
        ) as response:
            response.raise_for_status()

            filename = _extract_filename(response, url)
            filepath = os.path.join(dest_dir, filename)

            with open(filepath, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    _validate_pdf(filepath)
    return filepath
