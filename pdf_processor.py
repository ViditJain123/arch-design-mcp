"""
pdf_processor.py — Importable library for splitting PDFs into page images.

Refactored from split_pdf.py: no CLI concerns, no print statements, no sys.exit.
Raises exceptions on failure instead.
"""

import json
import os
import time
from pathlib import Path


def get_pdf_page_count(pdf_path: str) -> int:
    """Get page count without loading the full PDF into memory."""
    from pdf2image.pdf2image import pdfinfo_from_path

    try:
        info = pdfinfo_from_path(pdf_path)
        return info.get("Pages", 0)
    except Exception:
        try:
            from pypdf import PdfReader

            reader = PdfReader(pdf_path)
            return len(reader.pages)
        except Exception:
            return 0


def parse_page_range(range_str: str, max_pages: int) -> list[int]:
    """
    Parse a page range string into a sorted list of page numbers.

    Supports:
        "1-10"         -> pages 1 through 10
        "5,10,15"      -> specific pages
        "1-5,10,20-25" -> mixed ranges and specific pages
    """
    pages = set()
    parts = range_str.split(",")

    for part in parts:
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start = max(1, int(start.strip()))
            end = min(max_pages, int(end.strip()))
            pages.update(range(start, end + 1))
        else:
            p = int(part.strip())
            if 1 <= p <= max_pages:
                pages.add(p)

    return sorted(pages)


def split_pdf(
    input_path: str,
    output_dir: str,
    dpi: int = 200,
    max_dimension: int = 4096,
    fmt: str = "png",
    pages: str | None = None,
) -> dict:
    """
    Split a PDF into individual page images.

    Processes one page at a time to handle very large files without
    running out of memory.

    Args:
        input_path: Path to source PDF
        output_dir: Directory to write page images
        dpi: Render resolution (150=fast, 200=recommended, 300=high detail)
        max_dimension: Max pixels on longest side (Claude vision limit ~4096)
        fmt: Output format (png or jpg)
        pages: Optional page range string, e.g. "1-10" or "5,10,15"

    Returns:
        Manifest dict with processing results.

    Raises:
        FileNotFoundError: If input_path doesn't exist.
        RuntimeError: If no pages could be processed.
    """
    from pdf2image import convert_from_path
    from PIL import Image

    input_path = os.path.abspath(input_path)
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
    total_pages = get_pdf_page_count(input_path)

    if total_pages == 0:
        raise RuntimeError(f"Could not determine page count for: {input_path}")

    # Determine which pages to process
    page_list = None
    if pages:
        page_list = parse_page_range(pages, total_pages)

    manifest = {
        "source_file": os.path.basename(input_path),
        "source_path": input_path,
        "source_size_mb": round(file_size_mb, 2),
        "total_pages": total_pages,
        "dpi": dpi,
        "max_dimension": max_dimension,
        "format": fmt,
        "pages_processed": [],
        "errors": [],
    }

    total_image_size = 0
    start_time = time.time()

    pages_to_process = page_list if page_list else range(1, total_pages + 1)

    for page_num in pages_to_process:
        try:
            images = convert_from_path(
                input_path,
                dpi=dpi,
                first_page=page_num,
                last_page=page_num,
                fmt=fmt,
                thread_count=2,
            )

            if not images:
                manifest["errors"].append(
                    {"page": page_num, "error": "No image returned"}
                )
                continue

            img = images[0]

            # Downscale if exceeds max dimension
            w, h = img.size
            longest = max(w, h)
            if longest > max_dimension:
                scale = max_dimension / longest
                new_w = int(w * scale)
                new_h = int(h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                w, h = new_w, new_h

            # Save
            filename = f"page_{page_num:03d}.{fmt}"
            filepath = os.path.join(output_dir, filename)

            if fmt in ("jpg", "jpeg"):
                img.save(filepath, "JPEG", quality=90, optimize=True)
            else:
                img.save(filepath, "PNG", optimize=True)

            img_size = os.path.getsize(filepath)
            total_image_size += img_size

            manifest["pages_processed"].append(
                {
                    "page_number": page_num,
                    "filename": filename,
                    "width": w,
                    "height": h,
                    "size_kb": round(img_size / 1024, 1),
                }
            )

            del img, images

        except Exception as e:
            manifest["errors"].append({"page": page_num, "error": str(e)})

    elapsed = time.time() - start_time
    total_image_size_mb = total_image_size / (1024 * 1024)

    manifest["total_image_size_mb"] = round(total_image_size_mb, 2)
    manifest["processing_time_seconds"] = round(elapsed, 1)
    manifest["pages_succeeded"] = len(manifest["pages_processed"])
    manifest["pages_failed"] = len(manifest["errors"])

    # Write manifest
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest
