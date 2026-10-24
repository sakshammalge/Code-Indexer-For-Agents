"""Tool implementations shared by the MCP server and the CLI. All return compact plain text."""

from __future__ import annotations

import re

from .context import lang_name, render_overview
from .index import Index
from .scanner import is_binary

READ_DEFAULT_LINES = 250
READ_MAX_LINES = 1000


def overview(index: Index) -> str:
    return render_overview(index, max_files=60, tree_depth=3, max_symbols=10,
                           include_mcp_note=False, heading="# Project overview")


def search_code(index: Index, query: str, path_glob: str | None = None, lang: str | None = None,
                limit: int = 10) -> str:
    hits = index.search(query, path_glob=path_glob or None, lang=lang or None, limit=limit)
    if not hits:
        filt = "".join([f" path_glob={path_glob!r}" if path_glob else "", f" lang={lang!r}" if lang else ""])
        return f"No matches for {query!r}{filt}. Try fewer/other keywords, or find_symbol for definitions."
    out = []
    for h in hits:
        where = f" — in {h['symbol']}" if h["symbol"] else ""
        out.append(f"{h['path']}:{h['start_line']}-{h['end_line']} ({h['lang']}){where}")
        for n, text in h["lines"]:
            out.append(f"  {n:>5}| {text}")
    return "\n".join(out)


def find_symbol(index: Index, name: str, kind: str | None = None, limit: int = 30) -> str:
    rows = index.find_symbol(name, kind=kind or None, limit=limit)
    if not rows:
        return f"No symbol matching {name!r}" + (f" (kind={kind!r})" if kind else "") + \
            ". Try search_code for a keyword search."
    out = []
    for r in rows:
        qual = f"{r['parent']}.{r['name']}" if r["parent"] and r["kind"] != "section" else r["name"]
        rng = f"{r['line']}" + (f"-{r['end_line']}" if r["end_line"] and r["end_line"] != r["line"] else "")
        line = f"{r['kind']} {qual} — {r['path']}:{rng}"
        if r["signature"] and r["signature"] != r["name"]:
            line += f"\n    {r['signature']}"
        if r["docstring"]:
            line += f"\n    {r['docstring']}"
        out.append(line)
    return "\n".join(out)


def file_outline(index: Index, path: str) -> str:
    rel = index.resolve_path(path)
    if rel is None:
        return f"File not indexed: {path!r} (paths are relative to {index.root})."
    f = index.file_row(rel)
    syms = index.symbols_for(rel)
    head = f"{rel} — {lang_name(f['lang'])}, {f['lines']} lines"
    if f["doc"]:
        head += f"\n{f['doc']}"
    if f["entry"]:
        head += f"\n[entry: {f['entry']}]"
    if not syms:
        return head + "\n(no symbols extracted)"
    out = [head]
    stack: list[int] = []  # end lines of open containers
    for s in syms:
        while stack and stack[-1] < s["line"]:
            stack.pop()
        depth = len(stack)
        end = s["end_line"] or s["line"]
        rng = f"L{s['line']}" + (f"-{end}" if end != s["line"] else "")
        sig = s["signature"] or s["name"]
        label = sig if re.search(rf"\b{re.escape(s['kind'])}\b", sig) else f"{s['kind']} {sig}"
        line = f"{'  ' * depth}{rng} {label}"
        if s["docstring"]:
            line += f"  — {s['docstring']}"
        out.append(line)
        if end > s["line"]:
            stack.append(end)
    return "\n".join(out)


def read_file(index: Index, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    absp = index.safe_abspath(path)
    if absp is None:
        return f"Refusing to read {path!r}: outside the project root {index.root}."
    if not absp.is_file():
        rel = index.resolve_path(path)
        absp = index.safe_abspath(rel) if rel else None
        if absp is None or not absp.is_file():
            return f"No such file: {path!r}."
    data = absp.read_bytes()
    if is_binary(data):
        return f"{path} looks like a binary file; not shown."
    lines = data.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    if end_line:
        end = min(total, int(end_line), start + READ_MAX_LINES - 1)
    else:
        end = min(total, start + READ_DEFAULT_LINES - 1)
    rel = absp.relative_to(index.root).as_posix()
    if start > total:
        return f"{rel} has only {total} lines."
    width = len(str(end))
    body = "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))
    note = ""
    if end < total:
        note = f"\n… ({total - end} more lines; call read_file with start_line={end + 1})"
    return f"{rel} (lines {start}-{end} of {total})\n{body}{note}"
