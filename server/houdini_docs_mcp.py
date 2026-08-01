#!/usr/bin/env python3
"""
Houdini local docs MCP server for Claude Desktop.

Setup:
    uv init houdini-docs-mcp
    cd houdini-docs-mcp
    uv add "mcp[cli]" httpx beautifulsoup4

Doc server resolution (no manual step required in the common case):
    1. Houdini's embedded help server, which auto-starts when the app is open.
       Default port 48626. This is the zero-effort path -- just open Houdini.
    2. Fallback: a manually started `hhelp serve --host=127.0.0.1 --port=8080`
       for headless / dev setups where the embedded UI server isn't present.

    To confirm the exact port your running Houdini serves on, run this in the
    Houdini Python Shell:
        import hou; print(hou.ui.helpServerUrl())
    If it differs from the defaults below, add it to DOC_CANDIDATES.
"""

import os
import sys
import logging
from mcp.server.fastmcp import FastMCP
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("houdini-docs")

mcp = FastMCP("houdini-docs")

# ---------------------------------------------------------------------------
# Doc-server endpoint resolution
# ---------------------------------------------------------------------------
# Priority order. 48626 = Houdini's embedded help server (auto-runs when the
# app is open, UI available). 8080 = manual `hhelp serve` fallback.
# Override or extend via env var HOUDINI_DOCS_URLS (comma-separated).
_DEFAULT_CANDIDATES = [
    "http://127.0.0.1:48626",
    "http://127.0.0.1:8080",
]

_env = os.environ.get("HOUDINI_DOCS_URLS", "").strip()
DOC_CANDIDATES = (
    [u.strip().rstrip("/") for u in _env.split(",") if u.strip()]
    if _env
    else _DEFAULT_CANDIDATES
)

# /_search params
# sequence: arbitrary int, just needs to be present
# category: empty = all, or 'vex', 'node/sop', 'node/dop', '_' (user guide), etc.
SEARCH_PARAMS = "sequence=1&permanent=false&lang=en"

PROBE_TIMEOUT = 1.5   # seconds -- probe must be cheap; runs on cold cache only
FETCH_TIMEOUT = 5     # seconds -- real content fetches

NOT_REACHABLE = (
    "No Houdini doc server reachable.\n"
    "  - Open Houdini (embedded help server auto-starts on port 48626), or\n"
    "  - run `hhelp serve --host=127.0.0.1 --port=8080` manually.\n"
    "If Houdini is open on a non-default port, run "
    "`import hou; print(hou.ui.helpServerUrl())` in the Python Shell and set "
    "HOUDINI_DOCS_URLS to that base URL."
)

# Session cache of the resolved base URL. Re-verified cheaply on each use so a
# close/reopen on a different port (or a stopped server) self-heals.
_resolved_base: str | None = None


def _probe(base: str) -> bool:
    """True if a Houdini doc server answers at `base`.

    Uses /_search as the liveness endpoint: a 200 confirms it's the help
    server rather than some unrelated service squatting the port.
    """
    url = f"{base}/_search?q=point&category=&{SEARCH_PARAMS}"
    try:
        r = httpx.get(url, timeout=PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


def resolve_base() -> str | None:
    """Return a live doc-server base URL, caching it for the session.

    Returns None if nothing is reachable, so callers can emit NOT_REACHABLE.
    """
    global _resolved_base

    # Fast path: re-verify the cached endpoint. If Houdini was closed/reopened
    # elsewhere, this fails and we fall through to a full re-probe.
    if _resolved_base is not None:
        if _probe(_resolved_base):
            return _resolved_base
        log.info("Cached doc server %s went away, re-probing", _resolved_base)
        _resolved_base = None

    for base in DOC_CANDIDATES:
        if _probe(base):
            log.info("Resolved Houdini doc server: %s", base)
            _resolved_base = base
            return base

    log.warning("No Houdini doc server reachable among %s", DOC_CANDIDATES)
    return None


def raw_fetch(path_or_url: str) -> str | None:
    """Fetch content. Absolute URLs pass through; anything else is resolved
    against the live doc-server base. Returns None on any failure.
    """
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        base = resolve_base()
        if base is None:
            return None
        url = f"{base}/{path_or_url.lstrip('/')}"
    try:
        r = httpx.get(url, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        return r.text
    except httpx.ConnectError:
        # A previously-resolved server may have just died mid-session; drop the
        # cache so the next call re-probes instead of hammering a dead port.
        global _resolved_base
        _resolved_base = None
        return None
    except httpx.HTTPStatusError:
        return None
    except Exception:
        return None


def clean_html(html: str) -> str:
    """Strip chrome, return readable text from main content."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()
    main = (
        soup.find("div", class_="nd-main-content")
        or soup.find("div", id="content")
        or soup.find("article")
        or soup.find("main")
        or soup.body
    )
    return main.get_text(separator="\n", strip=True) if main else soup.get_text(strip=True)


def parse_search_fragment(html: str) -> str:
    """
    Parse the /_search HTML fragment.

    Structure:
      .instants      -- top instant matches (VEX functions show signatures here)
      .search-category sections -- categorized hits with href paths
    """
    soup = BeautifulSoup(html, "html.parser")
    lines = []

    # Stats
    stats = soup.find("p", class_="stats")
    if stats:
        lines.append(stats.get_text(strip=True))
        lines.append("")

    # Instant results -- highest value, include inline signatures
    instants = soup.find("div", class_="instants")
    if instants:
        lines.append("=== TOP MATCHES ===")
        for hit in instants.find_all("div", class_="hit"):
            a = hit.find("a", class_="label")
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href", "").lstrip("/")
            desc = hit.find("small", class_="desc")
            desc_text = desc.get_text(strip=True) if desc else ""
            lines.append(f"{title}  [{desc_text}]  →  {href}")
            # Include any code signatures shown inline
            for code in hit.find_all("code"):
                lines.append(f"    {code.get_text(strip=True)}")
        lines.append("")

    # Categorized results
    for section in soup.find_all("section", class_="search-category"):
        heading = section.find("h2", class_="category")
        if heading:
            lines.append(f"=== {heading.get_text(strip=True)} ===")
        for hit in section.find_all("div", class_="hit"):
            # skip "more" expanders
            if "more" in hit.get("class", []):
                more_text = hit.get_text(strip=True)
                lines.append(f"  ({more_text})")
                continue
            a = hit.find("a", class_="label")
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href", "").lstrip("/")
            desc = hit.find("small", class_="desc")
            desc_text = desc.get_text(strip=True) if desc else ""
            lines.append(f"  {title}  [{desc_text}]  →  {href}")
        lines.append("")

    return "\n".join(lines).strip() if lines else "No results found."


@mcp.tool()
def search_docs(query: str, category: str = "") -> str:
    """
    Search local Houdini docs via /_search endpoint.
    Returns instant matches (with VEX signatures inline) and categorized results with exact paths.
    Use returned paths with get_doc_page() -- never guess paths.

    Args:
        query:    search terms, e.g. 'pcfind', 'muscle solver', 'vellum constraint weld'
        category: optional filter: 'vex', 'node/sop', 'node/dop', 'node/vop',
                  '_' (user guide), 'tool', 'example', 'hscript', 'hommethod'
                  leave empty for all categories
    """
    html = raw_fetch(f"_search?q={query}&category={category}&{SEARCH_PARAMS}")
    if html is None:
        return NOT_REACHABLE
    return parse_search_fragment(html)


@mcp.tool()
def get_doc_page(path: str) -> str:
    """
    Fetch a Houdini doc page by its exact path.
    Always get the path from search_docs() -- do not construct paths manually.

    Examples (as returned by search_docs):
        'vex/functions/pcfind'
        'nodes/sop/attribwrangle'
        'nodes/dop/femsolver'
        'finiteelements/solvemethod'
        'muscles/index'
    """
    path = path.strip("/")
    html = raw_fetch(path)
    if html is None:
        return f"404 or unreachable: /{path}\nUse search_docs() to find the correct path."
    return clean_html(html)


@mcp.tool()
def get_vex_function(name: str) -> str:
    """
    Fetch VEX function docs. Tries direct path first, falls back to search.
    For VEX functions, search_docs() also returns signatures inline in TOP MATCHES --
    often enough without needing get_doc_page() at all.

    Examples: name='pcfind', 'xyzdist', 'nearpoints', 'addpoint', 'primuv'
    """
    html = raw_fetch(f"vex/functions/{name}")
    if html is not None:
        return clean_html(html)

    log.info("Direct path failed for vex/functions/%s, falling back to search", name)
    html = raw_fetch(f"_search?q={name}&category=vex&{SEARCH_PARAMS}")
    if html is None:
        return NOT_REACHABLE
    return f"[Searched for '{name}' in VEX functions]\n\n" + parse_search_fragment(html)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
