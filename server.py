"""
MCP Server for Architectural Drawing Analysis.

Exposes tools to load PDFs, inspect metadata, and extract page ranges
as native PDF content for Claude's vision-based analysis.

Run: python -m mcp_server.server
"""
import logging
import sys
 
# --- Configure logging ---------------------------------------------------
# Level DEBUG = everything (request/response headers, URLs, redirects)
# Level INFO  = key events (download start, redirect chain, validation)
LOG_LEVEL = logging.DEBUG
 
# Log to stderr so it doesn't interfere with MCP stdio transport
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
 
# Quiet down httpx's own internal logging (very noisy at DEBUG)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
 
logger = logging.getLogger("arch-drawing-analyzer.server")
# --------------------------------------------------------------------------

import asyncio
import atexit
import base64
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .downloader import download_pdf
from . import graph_auth
from .graph_downloader import download_from_sharepoint, _is_sharepoint_url
from .pdf_processor import (
    inspect_pdf, extract_pages_pdf, extract_pages_images,
    save_pages_pdf, save_pages_images, search_text, page_range,
)

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "arch-drawing-analyzer",
    instructions=(
        "Architectural drawing PDF analyzer. "
        "IMPORTANT: When the user provides a URL or file path, call process_pdf "
        "directly with that exact input. Do NOT use Microsoft 365 tools to search "
        "for or re-fetch the file. "
        "If a SharePoint URL fails with an authentication error, use o365_login_start "
        "to begin sign-in, present the URL and code to the user, then call "
        "o365_login_complete once they confirm they have signed in. "
        "Workflow: "
        "1) process_pdf(url) — returns session_id and total page count instantly. "
        "2) save_pages(session_id, start_page=1, end_page=10) — writes a sliced PDF "
        "   to arch-design-mcp/pages/. Returns the file path. "
        "   Then use Filesystem tools to copy/read the file for analysis. "
        "   Or search_pdf(session_id, query) to find text across all pages. "
        "3) Repeat save_pages for the next batch until done. "
        "4) cleanup_session(session_id) when finished."
    ),
)

# Session storage: session_id -> {"temp_dir": str, "manifest": dict}
_sessions: dict[str, dict] = {}

# Analysis prompt path (resolved relative to project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ANALYSIS_PROMPT_PATH = _PROJECT_ROOT / "analysis_prompt.md"


def _cleanup_all_sessions():
    """Remove all temp directories on exit."""
    for session in _sessions.values():
        td = session.get("temp_dir")
        if td and os.path.isdir(td):
            shutil.rmtree(td, ignore_errors=True)


atexit.register(_cleanup_all_sessions)


# ---------------------------------------------------------------------------
# Resource: analysis prompt
# ---------------------------------------------------------------------------

@mcp.resource("analysis://prompt")
def analysis_prompt_resource() -> str:
    """The analysis prompt template for architectural drawing pages."""
    return _ANALYSIS_PROMPT_PATH.read_text()


# ---------------------------------------------------------------------------
# Tool: get_analysis_prompt
# ---------------------------------------------------------------------------

@mcp.tool()
def get_analysis_prompt() -> str:
    """
    Returns the analysis prompt template that describes how to analyze
    each architectural drawing page. Call this before analyzing pages.
    """
    return _ANALYSIS_PROMPT_PATH.read_text()


# ---------------------------------------------------------------------------
# Tools: O365 Authentication
# ---------------------------------------------------------------------------

@mcp.tool()
def o365_auth_status() -> dict:
    """
    Check whether the user is currently authenticated to Office 365 / SharePoint.
    Fast, no side effects — safe to call at any time.

    Returns:
        Dict with authenticated (bool) and username if authenticated.
    """
    try:
        app, cache = graph_auth._get_app()
        accounts = app.get_accounts()
        if not accounts:
            return {"authenticated": False}

        result = app.acquire_token_silent(graph_auth._SCOPES, account=accounts[0])
        if result and "access_token" in result:
            graph_auth._save_cache(cache)
            return {
                "authenticated": True,
                "username": accounts[0].get("username", "unknown"),
            }

        return {"authenticated": False, "reason": "token_expired"}
    except Exception as e:
        return {"authenticated": False, "error": str(e)}


@mcp.tool()
def o365_login_start() -> dict:
    """
    Start the Office 365 device code sign-in flow.
    Returns a URL and code that the user must visit to authenticate.
    After the user signs in, call o365_login_complete to finish.

    Returns:
        Dict with status, user_code, verification_uri_complete, and message.
        If already authenticated, returns status="already_authenticated".
    """
    return graph_auth.initiate_device_code()


@mcp.tool()
async def o365_login_complete(timeout_seconds: int = 90) -> dict:
    """
    Wait for the user to complete the device code sign-in started by o365_login_start.
    Blocks until the user finishes signing in or the timeout is reached.

    Args:
        timeout_seconds: Max seconds to wait (default 90).

    Returns:
        Dict with status ("authenticated", "timeout", or "error") and details.
    """
    return await graph_auth.complete_device_code(timeout_seconds=timeout_seconds)


@mcp.tool()
def o365_logout() -> dict:
    """
    Clear cached Office 365 tokens. The user will need to re-authenticate.

    Returns:
        Dict with confirmation message.
    """
    message = graph_auth.clear_cache()
    return {"status": "logged_out", "message": message}


# ---------------------------------------------------------------------------
# Tool: process_pdf
# ---------------------------------------------------------------------------

@mcp.tool()
async def process_pdf(
    url: str,
) -> dict:
    """
    Load a PDF from a URL or local file path and inspect it.
    Returns metadata instantly — no image conversion.
    Use get_pages to retrieve actual page content as PDF.

    Args:
        url: Local file path (e.g. c:\\path\\to\\file.pdf), SharePoint sharing link,
             or any HTTPS URL to a PDF.

    Returns:
        Dict with session_id, total_pages, source_file, size, and sample page dimensions.
    """
    session_id = uuid.uuid4().hex[:12]
    temp_dir = tempfile.mkdtemp(prefix=f"mcp_arch_{session_id}_")

    try:
        # Resolve the PDF path
        if os.path.isfile(url):
            pdf_path = url
        elif _is_sharepoint_url(url):
            pdf_path = await download_from_sharepoint(url, temp_dir)
        else:
            pdf_path = await download_pdf(url, temp_dir)

        # Fast metadata probe — no conversion
        info = await asyncio.to_thread(inspect_pdf, pdf_path)

        _sessions[session_id] = {"temp_dir": temp_dir, "pdf_path": pdf_path, "info": info}

        return {
            "session_id": session_id,
            **info,
        }

    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Tool: get_pages
# ---------------------------------------------------------------------------

@mcp.tool()
def get_pages(
    session_id: str,
    start_page: int,
    end_page: int,
    as_images: bool = False,
    dpi: int = 150,
    max_dimension: int = 4096,
) -> dict | list:
    """
    Extract pages from start_page to end_page (inclusive, 1-based).

    Default: returns base64 PDF (fast, instant extraction).
    With as_images=True: renders pages as PNG images for vision analysis
    (slower, but Claude can visually read the drawings).

    Args:
        session_id: The session ID returned by process_pdf.
        start_page: First page to extract (1-based).
        end_page: Last page to extract (inclusive).
                  For images: keep range to 1-3 pages to stay under size limits.
                  For PDF: up to 10 pages at a time.
        as_images: If True, convert pages to PNG images instead of PDF.
        dpi: Image render resolution (only used with as_images=True).
             150 is good for arch drawings. Use 100 for faster/smaller.
        max_dimension: Max pixels on longest side (only with as_images=True).

    Returns:
        If as_images=False: dict with pages_included, page_count, size_kb, base64_pdf.
        If as_images=True: list of dicts with page_number, width, height, size_kb,
                           mime_type, base64_png.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Unknown session_id: {session_id}")

    info = session["info"]
    pdf_path = session["pdf_path"]

    requested = page_range(start_page, end_page, info["total_pages"])

    if as_images:
        return extract_pages_images(pdf_path, requested, dpi=dpi, max_dimension=max_dimension)
    else:
        return extract_pages_pdf(pdf_path, requested)


# ---------------------------------------------------------------------------
# Tool: save_pages
# ---------------------------------------------------------------------------

_DEFAULT_OUTPUT_DIR = os.path.join(Path(__file__).resolve().parent, "pages")

@mcp.tool()
def save_pages(
    session_id: str,
    start_page: int,
    end_page: int,
    output_dir: str = "",
) -> dict:
    """
    Extract pages and save as a PDF file to disk.
    Use this instead of get_pages when you need to read the pages
    via filesystem tools (e.g. copy_file_user_to_claude).

    Args:
        session_id: The session ID returned by process_pdf.
        start_page: First page to extract (1-based).
        end_page: Last page to extract (inclusive).
        output_dir: Directory to save the PDF. Defaults to arch-design-mcp/pages.

    Returns:
        Dict with output_path, pages_included, page_count, and size_kb.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Unknown session_id: {session_id}")

    info = session["info"]
    pdf_path = session["pdf_path"]
    requested = page_range(start_page, end_page, info["total_pages"])

    dest_dir = output_dir.strip() if output_dir.strip() else _DEFAULT_OUTPUT_DIR
    source_name = os.path.splitext(info["source_file"])[0]
    filename = f"{source_name}_p{start_page}-{end_page}.pdf"
    output_path = os.path.join(dest_dir, filename)

    return save_pages_pdf(pdf_path, requested, output_path)


# ---------------------------------------------------------------------------
# Tool: save_pages_as_images
# ---------------------------------------------------------------------------

@mcp.tool()
def save_pages_as_images(
    session_id: str,
    start_page: int,
    end_page: int,
    output_dir: str = "",
    dpi: int = 150,
    max_dimension: int = 4096,
) -> list[dict]:
    """
    Render pages as PNG images and save to disk.
    Use this when Claude needs to visually read the drawings.
    After saving, use Filesystem copy_file_user_to_claude to pull
    the images into Claude's environment for viewing.

    Args:
        session_id: The session ID returned by process_pdf.
        start_page: First page to render (1-based).
        end_page: Last page to render (inclusive).
                  Keep to 1-3 pages at a time — arch drawings are large.
        output_dir: Directory to save PNGs. Defaults to arch-design-mcp/pages.
        dpi: Render resolution. 150 is good, 100 for faster/smaller.
        max_dimension: Max pixels on longest side.

    Returns:
        List of dicts with page_number, output_path, width, height, size_kb.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Unknown session_id: {session_id}")

    info = session["info"]
    pdf_path = session["pdf_path"]
    requested = page_range(start_page, end_page, info["total_pages"])

    dest_dir = output_dir.strip() if output_dir.strip() else _DEFAULT_OUTPUT_DIR

    return save_pages_images(pdf_path, requested, dest_dir, dpi=dpi, max_dimension=max_dimension)


# ---------------------------------------------------------------------------
# Tool: search_pdf
# ---------------------------------------------------------------------------

@mcp.tool()
def search_pdf(
    session_id: str,
    query: str,
    start_page: int = 1,
    end_page: int = 0,
    max_results: int = 50,
) -> dict:
    """
    Search for text within a processed PDF. Case-insensitive.
    Returns matching pages with context snippets and bounding boxes.

    Useful for finding specific sheets, details, equipment schedules,
    or any text content in drawing sets without rendering every page.

    Note: architectural drawings are mostly vector graphics with limited
    embedded text. Results depend on how the PDF was generated.

    Args:
        session_id: The session ID returned by process_pdf.
        query: Text to search for (case-insensitive).
        start_page: First page to search (1-based). Default: 1.
        end_page: Last page to search (inclusive). Default: 0 = all pages.
        max_results: Maximum number of matches to return. Default: 50.

    Returns:
        Dict with query, pages_searched, total_matches, and matches list.
        Each match has page_number, text_snippet, and bbox.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Unknown session_id: {session_id}")

    info = session["info"]
    pdf_path = session["pdf_path"]
    ep = end_page if end_page > 0 else info["total_pages"]

    return search_text(pdf_path, query, start_page=start_page, end_page=ep, max_results=max_results)


# ---------------------------------------------------------------------------
# Tool: cleanup_session
# ---------------------------------------------------------------------------

@mcp.tool()
def cleanup_session(session_id: str) -> str:
    """
    Delete temporary files for a completed analysis session.

    Args:
        session_id: The session ID to clean up.

    Returns:
        Confirmation message.
    """
    session = _sessions.pop(session_id, None)
    if not session:
        return f"Session {session_id} not found (already cleaned up?)."

    temp_dir = session.get("temp_dir")
    if temp_dir and os.path.isdir(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)

    return f"Session {session_id} cleaned up successfully."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print('running')
    mcp.run(transport="stdio")
