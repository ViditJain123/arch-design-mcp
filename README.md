# Architectural Drawing Analyzer — Setup & Usage Guide

## What This Is

An MCP server that lets Claude read and analyze large architectural drawing PDFs (permit sets, construction documents, etc.) that are too big to upload directly. It works by slicing PDFs into manageable chunks — either as extracted PDF pages or rendered PNG images — and bridging them into Claude's environment via the Filesystem connector.

Supports local files and authenticated SharePoint downloads (via Microsoft Graph API with device code auth).

## System Components

### 1. arch-drawing-analyzer (MCP Server)

**Install location:** `%LOCALAPPDATA%\arch-design-mcp`

> On Windows this expands to `C:\Users\{username}\AppData\Local\arch-design-mcp`. Per-user, no admin rights required.

A Python MCP server running over stdio transport. Provides seven tools:

| Tool | Purpose | Speed |
|------|---------|-------|
| `process_pdf` | Load a PDF (local path, SharePoint link, or URL), return metadata + session_id | Instant |
| `save_pages` | Extract a page range to a new PDF file on disk | Instant |
| `save_pages_as_images` | Render a page range as PNG files on disk | ~1-2 sec/page |
| `get_pages` | Return page content as base64 (PDF or PNG) in the tool result | Instant (PDF) / slow (images) |
| `search_pdf` | Search for text across all pages, returns page numbers + context | Fast (~1-5 sec for 250 pages) |
| `get_analysis_prompt` | Return the analysis prompt template | Instant |
| `cleanup_session` | Delete temp files for a session | Instant |

**Source files:**

| File | Purpose |
|------|---------|
| `server.py` | MCP tool definitions, session management, FastMCP entry point |
| `pdf_processor.py` | PDF inspection (pypdf), page extraction, image rendering (PyMuPDF), text search |
| `downloader.py` | Unauthenticated URL download with logging |
| `graph_auth.py` | Microsoft Graph device code authentication, token caching/refresh |
| `graph_downloader.py` | Authenticated SharePoint file download via Graph API |
| `pyproject.toml` | Dependencies and Python version |
| `.env` | Azure AD credentials (not committed to git) |

### 2. Filesystem Connector (Claude Desktop Built-in)

Provides `copy_file_user_to_claude` which bridges files from the user's machine into Claude's environment. After `save_pages_as_images` writes PNGs to disk, the Filesystem connector copies them to `/mnt/user-data/uploads/` where Claude can view them.

**Required allowed directories:**

- `%LOCALAPPDATA%\arch-design-mcp` — server source + `pages/` output directory

### 3. Output Directory

All saved pages and images go to `%LOCALAPPDATA%\arch-design-mcp\pages\` by default. This keeps output contained within the Filesystem connector's allowed root. All tools accept an optional `output_dir` parameter to override this.

---

## Installation

### Prerequisites

- Python 3.13+ (via [uv](https://docs.astral.sh/uv/))
- Claude Desktop with Filesystem connector enabled
- Azure AD app registration (for SharePoint access — see SharePoint Setup below)

No external binaries (Poppler, etc.) are required. PyMuPDF handles all PDF rendering natively.

### Step 1: Clone the Project

```
cd %LOCALAPPDATA%
git clone <repo-url> arch-design-mcp
cd arch-design-mcp
```

### Step 2: Install Dependencies

```
uv sync
```

This installs from `pyproject.toml`:

| Package | Purpose |
|---------|---------|
| `fastmcp` | MCP server framework |
| `httpx` | Async HTTP client for URL downloads |
| `pymupdf` | PDF rendering and text search (no external binaries) |
| `pypdf` | Fast PDF page extraction (no rendering) |
| `msal` | Microsoft Authentication Library (device code flow) |
| `python-dotenv` | Load Azure credentials from `.env` file |

### Step 3: Register in Claude Desktop

Add to your Claude Desktop config file (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "arch-drawing-analyzer": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\Users\\YOUR_USERNAME\\AppData\\Local\\arch-design-mcp",
        "python",
        "-m",
        "server"
      ]
    }
  }
}
```

> **Replace `YOUR_USERNAME`** with your Windows username. The JSON config does not expand environment variables.

Merge this into any existing `mcpServers` block — don't replace the whole file if you have other servers configured.

### Step 4: Configure Filesystem Connector

In Claude Desktop, add `%LOCALAPPDATA%\arch-design-mcp` to the Filesystem connector's allowed directories.

---

## SharePoint Setup (Optional)

Required only if you want to pass SharePoint links directly to `process_pdf` instead of downloading files manually first.

### Azure AD App Registration

1. Go to [Azure Portal](https://portal.azure.com) → App registrations → find or create **SP-MCP**
2. **API Permissions** — add these Microsoft Graph delegated permissions:

| Permission | Type | Purpose |
|-----------|------|---------|
| `User.Read` | Delegated | Basic sign-in |
| `Files.Read.All` | Delegated | Read files the user can access |
| `Sites.Read.All` | Delegated | Resolve SharePoint sharing links |

3. Click **"Grant admin consent for {org}"** after adding permissions
4. Go to **Authentication** → **Advanced settings** → set **"Allow public client flows"** to **Yes** (required for device code flow)
5. Copy the **Application (client) ID** and **Directory (tenant) ID** from the Overview page

### Configure Credentials

```
cd %LOCALAPPDATA%\arch-design-mcp
copy .env.example .env
```

Edit `.env`:

```
SP_MCP_CLIENT_ID=your-actual-client-id
SP_MCP_TENANT_ID=your-actual-tenant-id
```

No client secret is needed. The device code flow uses a "public client" — no secrets stored on user machines.

### First Authentication

Before using SharePoint links, authenticate once by opening a terminal and running:

```
cd %LOCALAPPDATA%\arch-design-mcp
uv run python -m graph_auth
```

This will automatically open your browser to the Microsoft sign-in page with the device code pre-filled. Just sign in with your Tocci account. The terminal will confirm:

```
Opening browser for sign-in...
Waiting for sign-in to complete...

Authenticated as: psavine@tocci.com
Token cached at: C:\Users\psavine\.arch-design-mcp-token-cache.json
You can now use SharePoint links in Claude.
```

The token refreshes silently for ~90 days. If it expires, Claude will tell you to run the auth command again.

To clear the token cache: `uv run python -m graph_auth --clear`

### Security Notes

- **Delegated permissions only** — the server can only access files the signed-in user already has permission to view. No tenant-wide escalation.
- **No client secret** — nothing sensitive stored on the machine beyond a refresh token.
- **No open ports** — all auth happens via browser redirect, not a local server.
- **Token cache** — stored in the user's home directory, scoped to that user.

---

## Usage Workflows

### Workflow A: Visual Analysis (Claude reads the drawings)

The primary workflow. Claude renders specific pages as PNG images, copies them to its environment, and views them.

```
1. process_pdf("c:\path\to\drawings.pdf")
   → session_id, 255 pages, 69 MB

2. save_pages_as_images(session_id, start_page=1, end_page=1, dpi=150)
   → %LOCALAPPDATA%\arch-design-mcp\pages\page_001.png (707 KB)

3. Filesystem:copy_file_user_to_claude → copies PNG to Claude's environment

4. Claude views the drawing
```

**Limits:** 1-3 pages at a time for arch E-size (36"x24") sheets at 150 DPI. Each page is ~300-700 KB as PNG.

### Workflow B: Text Search (find pages without rendering)

Use `search_pdf` to locate specific content across all pages, then render only the relevant ones.

```
1. process_pdf("c:\path\to\drawings.pdf")
   → session_id, 255 pages

2. search_pdf(session_id, query="generator")
   → 3 matches: pages 178, 180, 195 with context snippets

3. save_pages_as_images(session_id, start_page=178, end_page=178)
   → render only the relevant sheet
```

Works best on PDFs with embedded text (specifications, schedules, title blocks). Architectural vector drawings have limited searchable text.

### Workflow C: Drawing Index Strategy

For large sets (100+ pages), don't render every page. Instead:

1. Render pages 2-4 to find the drawing index/sheet list
2. Identify sheet numbers for the discipline you need
3. Binary-search or text-search to the right page range
4. Render only the target sheets

### Workflow D: PDF Extraction (for archiving or re-upload)

Fast page extraction without image rendering.

```
1. process_pdf("c:\path\to\drawings.pdf")
   → session_id, 255 pages

2. save_pages(session_id, start_page=1, end_page=10)
   → %LOCALAPPDATA%\arch-design-mcp\pages\drawings_p1-10.pdf (2 MB)
```

### Workflow E: SharePoint Links

Pass SharePoint sharing links directly — the server authenticates and downloads automatically.

```
1. process_pdf("https://toccibuilding.sharepoint.com/:b:/s/24061-SouthShore/...")
   → authenticates via cached token, downloads, returns session_id
```

---

## Configuration

### Logging

Configured at the top of `server.py`. Logs go to stderr (doesn't interfere with MCP stdio transport).

```python
LOG_LEVEL = logging.DEBUG   # DEBUG for full diagnostics, INFO for production
```

### Image Rendering Defaults

| Parameter | Default | Notes |
|-----------|---------|-------|
| `dpi` | 150 | Good balance for arch drawings. Use 100 for faster/smaller. |
| `max_dimension` | 4096 | Max pixels on longest side. Claude vision limit is ~4096. |

A 36"x24" sheet at 150 DPI renders to ~5400x3600, then downscales to 4096x2730.

### MCP Client Timeout

The Claude.ai MCP client has a ~60-120 second timeout per tool call. Keep image rendering to 1-3 pages per call.

---

## Known Limitations

### MCP Tool Result Size

Base64-encoded content in tool results is limited to ~1 MB. A single 36"x24" arch drawing exceeds this. **Use** `save_pages_as_images` (writes to disk) **instead of** `get_pages(as_images=True)` (returns base64).

### Filesystem Bridge File Size

`copy_file_user_to_claude` fails on files larger than ~5 MB. **Keep** extracted PDF chunks to ~10 pages or use single-page image rendering.

### PDF Text Extraction

Architectural drawings are mostly vector graphics with limited embedded text. PyMuPDF text extraction is better than pypdf on these files, but results are still sparse on heavily vector-based sheets. Use visual analysis for reading drawings.

### Claude's PDF Handling

PDFs uploaded by the user in a chat message are rendered as images by the platform before Claude sees them. PDFs copied mid-conversation via `copy_file_user_to_claude` are NOT rendered — Claude sees raw bytes. That's why the PNG pipeline exists: render on the user's machine, copy the image, Claude views the image.

---

## File Structure

```
%LOCALAPPDATA%\arch-design-mcp\
├── server.py              # MCP server — tool definitions, session management
├── pdf_processor.py       # PDF inspection, extraction, rendering (PyMuPDF), text search
├── downloader.py          # Unauthenticated URL download with logging
├── graph_auth.py          # Device code auth, token caching/refresh (MSAL)
├── graph_downloader.py    # SharePoint download via Graph API
├── pyproject.toml         # Python dependencies
├── .env.example           # Credential template
├── .env                   # Actual credentials (git-ignored)
├── .gitignore
├── pages/                 # Default output for saved pages and images
├── __init__.py
├── .python-version        # Python 3.13
└── uv.lock                # Locked dependency versions
```

### Temp Files

Sessions create temp directories in `%TEMP%\mcp_arch_{session_id}_*`. Cleaned up by `cleanup_session()` or on server exit. Files in `pages/` persist — clean up manually or add to a maintenance routine.

### Token Cache

Stored at `~/.arch-design-mcp-token-cache.json`. Delete to force re-authentication.

---

## Deployment to Other Users

For non-technical users at Tocci:

1. Install [uv](https://docs.astral.sh/uv/) — single binary, no Python install needed
2. Clone the repo to `%LOCALAPPDATA%\arch-design-mcp`
3. Run `uv sync` — installs everything including Python
4. Copy `.env.example` to `.env`, fill in the Azure app credentials (same Client ID/Tenant ID for all users)
5. Add the `mcpServers` config to `%APPDATA%\Claude\claude_desktop_config.json` (see Step 3 above — replace `YOUR_USERNAME`)
6. Add `%LOCALAPPDATA%\arch-design-mcp` to Filesystem connector allowed directories
7. First SharePoint use: run `cd %LOCALAPPDATA%\arch-design-mcp && uv run python -m graph_auth` in a terminal, follow the browser prompt once

No Poppler install, no PATH configuration, no external binaries, no admin rights needed.
