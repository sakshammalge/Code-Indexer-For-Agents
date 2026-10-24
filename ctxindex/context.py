"""Render the project overview / PROJECT_MAP and write agent context + MCP config files."""

from __future__ import annotations

import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .index import INDEX_DIR, MARK_END, MARK_START, Index, find_managed_block

CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
MAP_FILE = "PROJECT_MAP.md"

LANG_NAMES = {
    "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript", "csharp": "C#",
    "html": "HTML", "css": "CSS", "scss": "SCSS", "markdown": "Markdown", "json": "JSON",
    "yaml": "YAML", "toml": "TOML", "msbuild": "MSBuild", "sln": "Solution", "shell": "Shell",
    "powershell": "PowerShell", "sql": "SQL", "xml": "XML", "razor": "Razor", "other": "Other",
}
_CALLABLE = {"function", "method", "constructor", "component", "mixin"}
_WEIGHT = {"class": 1.5, "interface": 1.2, "record": 1.0, "struct": 1.0, "enum": 0.6, "type": 0.6,
           "component": 1.2, "function": 1.0, "method": 0.3, "constructor": 0.1, "property": 0.1,
           "delegate": 0.5, "constant": 0.2, "variable": 0.1, "selector": 0.05, "mixin": 0.4,
           "section": 0.3, "heading": 0.2, "id": 0.1, "title": 0.5, "namespace": 0.0}
_SUMMARY_TIER = {"class": 0, "component": 0, "record": 1, "struct": 1, "interface": 1, "function": 1,
                 "mixin": 1, "enum": 2, "type": 2, "delegate": 2, "method": 2, "property": 3,
                 "selector": 3, "constant": 4, "variable": 4}
_SKIP_SUMMARY_KINDS = {"namespace", "title", "at-rule", "keyframes", "link", "constructor"}

MCP_NOTE = """**ctxindex MCP tools** (server `ctxindex`; the index refreshes itself before each query).
Prefer them over broad grep/glob scans:
- `project_overview()` — languages, layout, entry points, key files.
- `search_code(query, path_glob?, lang?, limit?)` — offline keyword search over code chunks (BM25); returns `path:line` hits with the enclosing symbol.
- `find_symbol(name, kind?)` — where a class/function/method/selector/heading is defined (`Class.method` works).
- `file_outline(path)` — a file's symbols with line ranges; read this before opening big files.
- `read_file(path, start_line?, end_line?)` — read just the line range you need.
- `reindex()` — force a refresh after large changes (e.g. branch switch)."""


def lang_name(lang: str | None) -> str:
    return LANG_NAMES.get(lang or "other", (lang or "other").capitalize())


# ---------------------------------------------------------------------------- building blocks


class _Model:
    """Everything the renderers need, loaded once from the index."""

    def __init__(self, index: Index):
        self.stats = index.stats()
        self.files = index.files()
        self.by_path: dict[str, list] = defaultdict(list)
        for s in index.all_symbols():
            self.by_path[s["path"]].append(s)

    def top_symbols(self, path: str) -> list[tuple[object, list]]:
        """Top-level symbols (C# namespace members count as top-level) with their children."""
        syms = self.by_path.get(path, [])
        names_ns = {s["name"] for s in syms if s["kind"] == "namespace"}
        top, children = [], defaultdict(list)
        for s in syms:
            p = s["parent"]
            if p is None or p in names_ns:
                if s["kind"] != "namespace":
                    top.append(s)
            else:
                children[p.split(".")[-1]].append(s)
        md_or_html = any(s["kind"] in ("section", "heading") for s in syms)
        if md_or_html:  # documents: show the top two heading levels
            top = [s for s in syms if s["kind"] in ("section", "heading") and
                   (s["signature"] or "").lstrip("<").startswith(("# ", "## ", "h1>", "h2>"))] or top
        return [(s, children.get(s["name"], [])) for s in top]

    def score(self, f) -> float:
        syms = self.by_path.get(f["path"], [])
        hi = sum(_WEIGHT.get(s["kind"], 0.2) for s in syms if _SUMMARY_TIER.get(s["kind"], 2) < 3)
        lo = sum(_WEIGHT.get(s["kind"], 0.2) for s in syms if _SUMMARY_TIER.get(s["kind"], 2) >= 3)
        sc = min(hi, 16.0) + min(lo, 2.0)
        entry = f["entry"] or ""
        if entry and entry != "build/config":
            sc += 8
        elif entry:
            sc += 2
        if f["doc"]:
            sc += 2
        parts = PurePosixPath(f["path"]).parts
        sc -= 0.7 * (len(parts) - 1)
        low = f["path"].lower()
        if any(t in low for t in ("test", "spec", "fixture", "mock", "example", "sample")):
            sc -= 5
        if PurePosixPath(low).name in ("readme.md", "agents.md", "claude.md", "contributing.md"):
            sc += 3
        return sc


def _sym_label(s, n_children: int = 0) -> str:
    name = s["name"]
    if s["kind"] in _CALLABLE:
        label = f"{name}()"
    elif s["kind"] in ("class", "interface", "record", "struct", "enum", "type", "delegate"):
        label = f"{s['kind'] if s['kind'] != 'class' else 'class'} {name}"
    else:
        label = name
    if n_children:
        label += f" [+{n_children}]"
    return label


def file_summary(model: _Model, f, max_symbols: int) -> str:
    items = [(s, ch) for s, ch in model.top_symbols(f["path"]) if s["kind"] not in _SKIP_SUMMARY_KINDS]
    is_doc = any(s["kind"] in ("section", "heading") for s, _ in items)
    if is_doc:
        items = [(s, []) for s, _ in items if s["name"] != f["doc"]]
    else:  # stable sort: types and callables before variables/constants/selectors
        items.sort(key=lambda it: _SUMMARY_TIER.get(it[0]["kind"], 2))
    seen: set[str] = set()
    unique = []
    for s, ch in items:  # overloads / repeated local helpers appear once
        if s["name"] not in seen:
            seen.add(s["name"])
            unique.append((s, ch))
    items = unique
    labels = [_sym_label(s, len(ch)) for s, ch in items[:max_symbols]]
    if len(items) > max_symbols:
        labels.append(f"… +{len(items) - max_symbols}")
    parts = []
    if f["doc"]:
        parts.append(f["doc"])
    if f["entry"]:
        parts.append(f"[{f['entry']}]")
    if labels:
        parts.append("· " + ", ".join(labels))
    return " ".join(parts)


def render_tree(paths: list[str], max_depth: int, max_entries: int) -> str:
    tree: dict = {}
    for p in paths:
        node = tree
        parts = p.split("/")
        for d in parts[:-1]:
            node = node.setdefault(d + "/", {})
        node.setdefault("\0files", []).append(parts[-1])

    def count(node) -> int:
        return len(node.get("\0files", [])) + sum(count(v) for k, v in node.items() if k != "\0files")

    out: list[str] = []

    def walk(node, depth: int, indent: str) -> None:
        dirs = sorted((k for k in node if k != "\0files"), key=str.lower)
        files = sorted(node.get("\0files", []), key=str.lower)
        entries = [("d", d) for d in dirs] + [("f", f) for f in files]
        shown = entries[:max_entries]
        for typ, name in shown:
            if typ == "d":
                sub = node[name]
                n = count(sub)
                out.append(f"{indent}{name}  ({n} file{'s' if n != 1 else ''})")
                if depth + 1 < max_depth:
                    walk(sub, depth + 1, indent + "  ")
            else:
                out.append(f"{indent}{name}")
        rest = entries[max_entries:]
        if rest:
            nd = sum(1 for t, _ in rest if t == "d")
            nf = len(rest) - nd
            bits = ([f"{nd} dirs"] if nd else []) + ([f"{nf} files"] if nf else [])
            out.append(f"{indent}… +{', '.join(bits)}")

    walk(tree, 0, "")
    return "\n".join(out)


def _languages_line(stats: dict) -> str:
    langs = [l for l in stats["languages"] if l["lang"] != "other"]
    other = next((l for l in stats["languages"] if l["lang"] == "other"), None)
    bits = [f"{lang_name(l['lang'])} {l['files']}" for l in langs]
    if other:
        bits.append(f"other {other['files']}")
    return " · ".join(bits)


def _entries(model: _Model, limit: int) -> list[str]:
    main = [f for f in model.files if f["entry"] and f["entry"] != "build/config"]
    cfg = [f for f in model.files if f["entry"] == "build/config"]
    main.sort(key=lambda f: (f["path"].count("/"), f["path"]))
    cfg.sort(key=lambda f: (f["path"].count("/"), f["path"]))
    out = [f"- `{f['path']}` — {f['entry']}" for f in main[:limit]]
    room = max(0, limit - len(out))
    if cfg and room:
        out.append("- build/config: " + ", ".join(f"`{f['path']}`" for f in cfg[:room]) +
                   (f" … +{len(cfg) - room}" if len(cfg) > room else ""))
    if len(main) > limit:
        out.append(f"- … +{len(main) - limit} more (see PROJECT_MAP.md)")
    return out


def render_overview(index: Index, *, max_files: int = 40, tree_depth: int = 2, max_symbols: int = 8,
                    include_mcp_note: bool = True, heading: str = "## Codebase index (ctxindex)") -> str:
    m = _Model(index)
    st = m.stats
    lines = [heading, ""]
    lines.append(f"_Auto-generated by ctxindex; regenerate with `ctxindex context`. "
                 f"Full map: `{INDEX_DIR}/{MAP_FILE}`._")
    lines.append("")
    lines.append(f"**Languages** ({st['files']} files, {st['symbols']} symbols): {_languages_line(st)}")
    lines.append("")
    lines.append(f"**Layout** (depth {tree_depth}):")
    lines.append("```")
    lines.append(render_tree([f["path"] for f in m.files], tree_depth, 14))
    lines.append("```")
    ent = _entries(m, 12)
    if ent:
        lines.append("")
        lines.append("**Entry points:**")
        lines.extend(ent)
    ranked = sorted((f for f in m.files if f["path"] not in CONTEXT_FILES and
                     (m.by_path.get(f["path"]) or f["doc"] or f["entry"])),
                    key=lambda f: (-m.score(f), f["path"]))
    chosen = sorted(ranked[:max_files], key=lambda f: f["path"])
    if chosen:
        lines.append("")
        extra = f"top {len(chosen)} of {len(ranked)}; " if len(ranked) > len(chosen) else ""
        lines.append(f"**Key files** ({extra}`name()` = callable, `[+N]` = members):")
        for f in chosen:
            summ = file_summary(m, f, max_symbols)
            line = f"- `{f['path']}`" + (f" — {summ}" if summ else "")
            lines.append(line if len(line) <= 260 else line[:259] + "…")
    if include_mcp_note:
        lines.append("")
        lines.append(MCP_NOTE)
    return "\n".join(lines)


def render_block(index: Index, **kw) -> str:
    return f"{MARK_START}\n{render_overview(index, **kw)}\n{MARK_END}"


def render_project_map(index: Index, *, max_symbols_per_file: int = 60, max_members: int = 30) -> str:
    m = _Model(index)
    st = m.stats
    out = ["# Project map", "",
           "_Auto-generated by ctxindex (`ctxindex context`). Do not edit by hand._", "",
           f"{st['files']} files · {st['lines']} lines · {st['symbols']} symbols", "",
           "## Languages", "", "| Language | Files | Lines |", "|---|---:|---:|"]
    for l in st["languages"]:
        out.append(f"| {lang_name(l['lang'])} | {l['files']} | {l['lines'] or 0} |")
    ent = _entries(m, 100)
    if ent:
        out += ["", "## Entry points", ""] + ent
    out += ["", "## Directory tree", "", "```", render_tree([f["path"] for f in m.files], 6, 60), "```",
            "", "## Files", ""]
    by_dir: dict[str, list] = defaultdict(list)
    for f in m.files:
        by_dir[str(PurePosixPath(f["path"]).parent)].append(f)
    for d in sorted(by_dir, key=lambda x: (x != ".", x.lower())):
        out.append(f"### {'(root)' if d == '.' else d + '/'}")
        out.append("")
        for f in by_dir[d]:
            head = f"- `{f['path']}` ({lang_name(f['lang'])}, {f['lines']} lines)"
            if f["doc"]:
                head += f" — {f['doc']}"
            if f["entry"]:
                head += f" [{f['entry']}]"
            out.append(head)
            top = [(s, ch) for s, ch in m.top_symbols(f["path"]) if s["kind"] != "namespace"]
            for s, ch in top[:max_symbols_per_file]:
                rng = f"L{s['line']}" + (f"-{s['end_line']}" if s["end_line"] and s["end_line"] != s["line"] else "")
                sig = s["signature"] or s["name"]
                item = f"  - {s['kind']} `{_clip(sig, 140)}` {rng}"
                if s["docstring"]:
                    item += f" — {_clip(s['docstring'], 120)}"
                out.append(item)
                if ch:
                    names = [_sym_label(c) for c in ch[:max_members]]
                    if len(ch) > max_members:
                        names.append(f"… +{len(ch) - max_members}")
                    out.append(f"    - members: {', '.join(names)}")
            if len(top) > max_symbols_per_file:
                out.append(f"  - … +{len(top) - max_symbols_per_file} more symbols (use `file_outline`)")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _clip(s: str, n: int) -> str:
    s = s.replace("`", "'")
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------- writing files


def upsert_block(path: Path, block: str) -> str:
    """Insert/replace the marker-delimited block in ``path``; text outside the markers is
    preserved byte-for-byte. Returns 'created' | 'updated' | 'unchanged'."""
    if path.exists():
        text = path.read_text(encoding="utf-8")
        span = find_managed_block(text)
        if span is not None:
            new = text[:span[0]] + block + text[span[1]:]
        else:
            if not text or text.endswith("\n\n"):
                sep = ""
            elif text.endswith("\n"):
                sep = "\n"
            else:
                sep = "\n\n"
            new = text + sep + block + "\n"
        if new == text:
            return "unchanged"
        path.write_text(new, encoding="utf-8")
        return "updated"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block + "\n", encoding="utf-8")
    return "created"


def write_context(index: Index, *, max_files: int = 40, tree_depth: int = 2,
                  targets: tuple[str, ...] = CONTEXT_FILES) -> dict[str, str]:
    results = _write_context_once(index, max_files, tree_depth, targets)
    # The context files are indexed too (file counts, their line counts in the map), so re-render
    # until the output is a fixed point: running `ctxindex context` again then changes nothing.
    for _ in range(3):
        if not index.update().changed:
            break
        again = _write_context_once(index, max_files, tree_depth, targets)
        for k, v in again.items():
            if v != "unchanged" and results.get(k) == "unchanged":
                results[k] = v
    return results


def _write_context_once(index: Index, max_files: int, tree_depth: int,
                        targets: tuple[str, ...]) -> dict[str, str]:
    results = {}
    block = render_block(index, max_files=max_files, tree_depth=tree_depth)
    for rel in targets:
        results[rel] = upsert_block(index.root / rel, block)
    map_path = index.dir / MAP_FILE
    new_map = render_project_map(index)
    old = map_path.read_text(encoding="utf-8") if map_path.exists() else None
    if old != new_map:
        map_path.write_text(new_map, encoding="utf-8")
        results[f"{INDEX_DIR}/{MAP_FILE}"] = "created" if old is None else "updated"
    else:
        results[f"{INDEX_DIR}/{MAP_FILE}"] = "unchanged"
    return results


def server_command() -> tuple[str, list[str]]:
    """How agents should launch the server: the `ctxindex` script if on PATH, else this Python."""
    if shutil.which("ctxindex"):
        return "ctxindex", ["serve"]
    return sys.executable, ["-m", "ctxindex", "serve"]


def _merge_json(path: Path, key: str, name: str, entry: dict) -> str:
    data: dict = {}
    existed = path.exists()
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            return "skipped (invalid JSON; not modified)"
        if not isinstance(data, dict):
            return "skipped (unexpected JSON; not modified)"
    servers = data.setdefault(key, {})
    if servers.get(name) == entry:
        return "unchanged"
    servers[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "updated" if existed else "created"


def write_mcp_configs(root: Path) -> dict[str, str]:
    cmd, args = server_command()
    return {
        # Claude Code: project-scoped servers. The server finds the project root by walking
        # up from its working directory to the nearest .ctxindex/ folder.
        ".mcp.json": _merge_json(root / ".mcp.json", "mcpServers", "ctxindex",
                                 {"type": "stdio", "command": cmd, "args": args}),
        # VS Code / GitHub Copilot agent mode.
        ".vscode/mcp.json": _merge_json(root / ".vscode" / "mcp.json", "servers", "ctxindex",
                                        {"type": "stdio", "command": cmd,
                                         "args": args + ["--root", "${workspaceFolder}"]}),
    }
