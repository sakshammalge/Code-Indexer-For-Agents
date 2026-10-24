"""Local stdio MCP server exposing the index to coding agents."""

from __future__ import annotations

import logging
from pathlib import Path

try:  # mcp SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import __version__, tools
from .index import Index

log = logging.getLogger("ctxindex")

INSTRUCTIONS = """ctxindex: an offline keyword + symbol index of this repository (no embeddings).
Start with project_overview for orientation. Use find_symbol to jump to definitions,
search_code for keyword/identifier search (path_glob like 'src/**/*.ts', lang like 'python'),
file_outline before reading large files, then read_file with a line range.
The index refreshes itself incrementally before queries; call reindex after big changes."""


def build_server(root: str | Path, refresh_interval: float = 10.0) -> _Server:
    index = Index(root)
    if index.is_empty():
        index.update()
    try:
        mcp = _Server(name="ctxindex", instructions=INSTRUCTIONS, version=__version__)
    except TypeError:  # older SDKs don't accept `version`
        mcp = _Server(name="ctxindex", instructions=INSTRUCTIONS)

    def fresh() -> Index:
        try:
            index.maybe_refresh(refresh_interval)
        except Exception:  # a failed refresh should not break the query
            log.exception("incremental reindex failed")
        return index

    @mcp.tool()
    def project_overview() -> str:
        """Languages and file counts, directory layout, entry points and one-line summaries of the
        most important files. Call this first to orient yourself in the codebase."""
        return tools.overview(fresh())

    @mcp.tool()
    def search_code(query: str, path_glob: str = "", lang: str = "", limit: int = 10) -> str:
        """Keyword search over the code (BM25 full-text, offline). Identifiers are matched by
        prefix and by camelCase/snake_case parts. Returns path:line ranges, the enclosing symbol
        and the matching lines.

        Args:
            query: keywords or identifiers, e.g. "retry backoff" or "parseConfig"; use quotes for a phrase.
            path_glob: optional glob to restrict files, e.g. "src/**/*.ts", "*.py", "tests/".
            lang: optional language filter: python, typescript, javascript, csharp, html, css, scss, markdown.
            limit: maximum number of hits (default 10, max 100).
        """
        return tools.search_code(fresh(), query, path_glob or None, lang or None, limit)

    @mcp.tool()
    def find_symbol(name: str, kind: str = "") -> str:
        """Find where a symbol is defined: exact name first, then prefix/substring/fuzzy matches.
        Accepts "Class.method". Shows kind, path:line range, signature and docstring.

        Args:
            name: symbol name, e.g. "UserService", "UserService.get_user", ".btn-primary", "--brand-color".
            kind: optional filter: class, interface, record, struct, enum, function, method, property,
                component, type, namespace, selector, variable, section ("function" also matches methods).
        """
        return tools.find_symbol(fresh(), name, kind or None)

    @mcp.tool()
    def file_outline(path: str) -> str:
        """Outline of one file: nested symbols with line ranges, signatures and docstrings.
        Cheaper than reading the whole file; use it to pick the lines to read_file.

        Args:
            path: project-relative path (a unique suffix such as "models.py" also works).
        """
        return tools.file_outline(fresh(), path)

    @mcp.tool()
    def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
        """Read a file (or a line range of it) with line numbers. Without end_line, returns up to
        250 lines from start_line; at most 1000 lines per call.

        Args:
            path: project-relative path.
            start_line: first line to return (1-based).
            end_line: last line to return (inclusive); 0 means start_line + 249.
        """
        return tools.read_file(index, path, start_line, end_line or None)

    @mcp.tool()
    def reindex(full: bool = False) -> str:
        """Re-scan the project now (incremental by default) and refresh the index.

        Args:
            full: re-parse every file even if unchanged.
        """
        st = index.update(full=full)
        return f"Reindexed {index.root}: {st}"

    return mcp


def serve(root: str | Path, refresh_interval: float = 10.0) -> None:
    logging.basicConfig(level=logging.WARNING, format="ctxindex: %(message)s")  # stderr only
    build_server(root, refresh_interval).run("stdio")
