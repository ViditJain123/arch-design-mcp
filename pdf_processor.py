"""
pdf_processor.py — PDF inspection, page extraction, image rendering, and text search.

Uses pypdf for fast page extraction and PyMuPDF (fitz) for rendering and text search.
No external binaries required (no Poppler).
"""

import base64
import io
import os

import fitz  # PyMuPDF
from pypdf import PdfReader, PdfWriter


def page_range(start_page: int, end_page: int, total_pages: int) -> list[int]:
    """
    Build a list of 1-based page numbers from start_page to end_page inclusive.
    Clamps to [1, total_pages].
    """
    start_page = max(1, start_page)
    end_page = min(total_pages, end_page)
    return list(range(start_page, end_page + 1))


def inspect_pdf(pdf_path: str) -> dict:
    """
    Quickly inspect a PDF and return metadata. No conversion.
    """
    pdf_path = os.path.abspath(pdf_path)
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Input file not found: {pdf_path}")

    file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    if total_pages == 0:
        raise RuntimeError(f"PDF has no pages: {pdf_path}")

    sample_indices = sorted(set([0, total_pages // 2, total_pages - 1]))
    page_samples = []
    for idx in sample_indices:
        page = reader.pages[idx]
        box = page.mediabox
        w = float(box.width)
        h = float(box.height)
        page_samples.append({
            "page_number": idx + 1,
            "width_pts": round(w, 1),
            "height_pts": round(h, 1),
            "width_in": round(w / 72, 1),
            "height_in": round(h / 72, 1),
        })

    return {
        "source_file": os.path.basename(pdf_path),
        "source_path": pdf_path,
        "source_size_mb": round(file_size_mb, 2),
        "total_pages": total_pages,
        "page_samples": page_samples,
    }


# ---------------------------------------------------------------------------
# Page extraction (pypdf — fast, no rendering)
# ---------------------------------------------------------------------------

def extract_pages_pdf(pdf_path: str, page_numbers: list[int]) -> dict:
    """Extract pages and return as base64-encoded PDF."""
    pdf_path = os.path.abspath(pdf_path)
    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for page_num in page_numbers:
        idx = page_num - 1
        if 0 <= idx < len(reader.pages):
            writer.add_page(reader.pages[idx])

    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    return {
        "pages_included": page_numbers,
        "page_count": len(page_numbers),
        "size_kb": round(len(pdf_bytes) / 1024, 1),
        "base64_pdf": base64.b64encode(pdf_bytes).decode("ascii"),
    }


def save_pages_pdf(pdf_path: str, page_numbers: list[int], output_path: str) -> dict:
    """Extract pages and write to a new PDF file on disk."""
    pdf_path = os.path.abspath(pdf_path)
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for page_num in page_numbers:
        idx = page_num - 1
        if 0 <= idx < len(reader.pages):
            writer.add_page(reader.pages[idx])

    with open(output_path, "wb") as f:
        writer.write(f)

    size_kb = round(os.path.getsize(output_path) / 1024, 1)

    return {
        "output_path": output_path,
        "pages_included": page_numbers,
        "page_count": len(page_numbers),
        "size_kb": size_kb,
    }


# ---------------------------------------------------------------------------
# Image rendering (PyMuPDF/fitz — no external binaries)
# ---------------------------------------------------------------------------

def _render_page(doc: fitz.Document, page_num: int, dpi: int, max_dimension: int) -> fitz.Pixmap:
    """Render a single page to a pixmap, downscaling if needed."""
    page = doc[page_num - 1]  # fitz is 0-based
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)

    # Downscale if longest side exceeds max_dimension
    longest = max(pix.width, pix.height)
    if longest > max_dimension:
        scale = max_dimension / longest
        new_w = int(pix.width * scale)
        new_h = int(pix.height * scale)
        # Re-render at adjusted zoom
        adjusted_zoom = zoom * scale
        mat = fitz.Matrix(adjusted_zoom, adjusted_zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)

    return pix


def save_pages_images(
    pdf_path: str,
    page_numbers: list[int],
    output_dir: str,
    dpi: int = 150,
    max_dimension: int = 4096,
) -> list[dict]:
    """Render pages as PNG files on disk."""
    pdf_path = os.path.abspath(pdf_path)
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    results = []

    doc = fitz.open(pdf_path)
    try:
        for page_num in page_numbers:
            try:
                pix = _render_page(doc, page_num, dpi, max_dimension)
                filepath = os.path.join(output_dir, f"page_{page_num:03d}.png")
                pix.save(filepath)
                size_kb = round(os.path.getsize(filepath) / 1024, 1)

                results.append({
                    "page_number": page_num,
                    "output_path": filepath,
                    "width": pix.width,
                    "height": pix.height,
                    "size_kb": size_kb,
                })
            except Exception as e:
                results.append({"page_number": page_num, "error": str(e)})
    finally:
        doc.close()

    return results


def extract_pages_images(
    pdf_path: str,
    page_numbers: list[int],
    dpi: int = 150,
    max_dimension: int = 4096,
) -> list[dict]:
    """Render pages as base64-encoded PNGs (returned in tool result)."""
    pdf_path = os.path.abspath(pdf_path)
    results = []

    doc = fitz.open(pdf_path)
    try:
        for page_num in page_numbers:
            try:
                pix = _render_page(doc, page_num, dpi, max_dimension)
                png_bytes = pix.tobytes("png")

                results.append({
                    "page_number": page_num,
                    "width": pix.width,
                    "height": pix.height,
                    "size_kb": round(len(png_bytes) / 1024, 1),
                    "mime_type": "image/png",
                    "base64_png": base64.b64encode(png_bytes).decode("ascii"),
                })
            except Exception as e:
                results.append({"page_number": page_num, "error": str(e)})
    finally:
        doc.close()

    return results


# ---------------------------------------------------------------------------
# Text search (PyMuPDF/fitz)
# ---------------------------------------------------------------------------

def search_text(
    pdf_path: str,
    query: str,
    start_page: int = 1,
    end_page: int | None = None,
    max_results: int = 50,
) -> dict:
    """
    Search for text across pages of a PDF.

    Args:
        pdf_path: Path to the PDF.
        query: Text to search for (case-insensitive).
        start_page: First page to search (1-based).
        end_page: Last page to search (inclusive). None = all pages.
        max_results: Max matches to return.

    Returns:
        Dict with query, total_matches, and matches list.
        Each match has page_number, text_snippet (context around the match),
        and bbox (x0, y0, x1, y1 in points).
    """
    pdf_path = os.path.abspath(pdf_path)
    doc = fitz.open(pdf_path)
    total_pages = len(doc)

    start_idx = max(0, start_page - 1)
    end_idx = min(total_pages, end_page) if end_page else total_pages

    matches = []
    try:
        for idx in range(start_idx, end_idx):
            if len(matches) >= max_results:
                break

            page = doc[idx]
            page_num = idx + 1

            # Search for all instances on this page
            found = page.search_for(query)
            if not found:
                continue

            # Get full page text for context snippets
            page_text = page.get_text("text")

            for rect in found:
                if len(matches) >= max_results:
                    break

                # Find the query in the page text for context
                query_lower = query.lower()
                text_lower = page_text.lower()
                pos = text_lower.find(query_lower)

                snippet = ""
                if pos >= 0:
                    ctx_start = max(0, pos - 60)
                    ctx_end = min(len(page_text), pos + len(query) + 60)
                    snippet = page_text[ctx_start:ctx_end].replace("\n", " ").strip()
                    if ctx_start > 0:
                        snippet = "..." + snippet
                    if ctx_end < len(page_text):
                        snippet = snippet + "..."

                matches.append({
                    "page_number": page_num,
                    "text_snippet": snippet,
                    "bbox": {
                        "x0": round(rect.x0, 1),
                        "y0": round(rect.y0, 1),
                        "x1": round(rect.x1, 1),
                        "y1": round(rect.y1, 1),
                    },
                })
    finally:
        doc.close()

    return {
        "query": query,
        "pages_searched": f"{start_page}-{end_idx}",
        "total_matches": len(matches),
        "matches": matches,
    }
