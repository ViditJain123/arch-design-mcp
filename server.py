"""
MCP Server for Architectural Drawing Analysis.

Exposes tools to download PDFs, split them into page images, and serve
base64-encoded images back to Claude for vision-based analysis.

Run: python -m mcp_server.server
"""

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
from .pdf_processor import split_pdf, parse_page_range

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "arch-drawing-analyzer",
    instructions=(
        "Architectural drawing PDF analyzer. Use process_pdf to download and split "
        "a PDF, then get_page_images to retrieve batches of page images for analysis. "
        "Call get_analysis_prompt first to know how to analyze each page."
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
# Tool: process_pdf
# ---------------------------------------------------------------------------

@mcp.tool()
async def process_pdf(
    url: str,
    dpi: int = 200,
    max_dimension: int = 4096,
) -> dict:
    """
    Download a PDF from a URL and split it into page images.

    Args:
        url: URL to the PDF file (SharePoint links supported).
        dpi: Render resolution. 200 recommended for architectural drawings.
        max_dimension: Max pixels on longest side (Claude vision limit ~4096).

    Returns:
        Dict with session_id, total_pages, pages_succeeded, pages_failed,
        and per-page metadata (no images — use get_page_images for those).
    """
    session_id = uuid.uuid4().hex[:12]
    temp_dir = tempfile.mkdtemp(prefix=f"mcp_arch_{session_id}_")

    try:
        # Download
        pdf_path = await download_pdf(url, temp_dir)

        # Split (blocking CPU work — run in thread)
        pages_dir = os.path.join(temp_dir, "pages")
        manifest = await asyncio.to_thread(
            split_pdf,
            input_path=pdf_path,
            output_dir=pages_dir,
            dpi=dpi,
            max_dimension=max_dimension,
        )

        _sessions[session_id] = {"temp_dir": temp_dir, "manifest": manifest}

        return {
            "session_id": session_id,
            "source_file": manifest["source_file"],
            "total_pages": manifest["total_pages"],
            "pages_succeeded": manifest["pages_succeeded"],
            "pages_failed": manifest["pages_failed"],
            "processing_time_seconds": manifest["processing_time_seconds"],
            "total_image_size_mb": manifest["total_image_size_mb"],
            "page_list": [
                {
                    "page_number": p["page_number"],
                    "width": p["width"],
                    "height": p["height"],
                    "size_kb": p["size_kb"],
                }
                for p in manifest["pages_processed"]
            ],
        }

    except Exception as e:
        # Clean up on failure
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Tool: get_page_images
# ---------------------------------------------------------------------------

@mcp.tool()
def get_page_images(session_id: str, pages: str) -> list:
    """
    Get base64-encoded page images for a batch of pages.

    Args:
        session_id: The session ID returned by process_pdf.
        pages: Page range string, e.g. "1-10", "5,10,15", "11-20".

    Returns:
        List of dicts, each with page_number, width, height, and
        base64_png (the base64-encoded PNG image data).
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Unknown session_id: {session_id}")

    manifest = session["manifest"]
    temp_dir = session["temp_dir"]
    pages_dir = os.path.join(temp_dir, "pages")

    requested = parse_page_range(pages, manifest["total_pages"])

    # Build lookup from page number to processed page info
    processed = {p["page_number"]: p for p in manifest["pages_processed"]}

    results = []
    for page_num in requested:
        page_info = processed.get(page_num)
        if not page_info:
            results.append(
                {
                    "page_number": page_num,
                    "error": "Page was not successfully processed",
                }
            )
            continue

        filepath = os.path.join(pages_dir, page_info["filename"])
        if not os.path.exists(filepath):
            results.append(
                {"page_number": page_num, "error": "Image file not found"}
            )
            continue

        with open(filepath, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("ascii")

        results.append(
            {
                "page_number": page_num,
                "width": page_info["width"],
                "height": page_info["height"],
                "size_kb": page_info["size_kb"],
                "mime_type": "image/png",
                "base64_png": img_data,
            }
        )

    return results


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
    mcp.run(transport="stdio")
