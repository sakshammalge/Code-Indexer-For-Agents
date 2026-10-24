# ctxindex

Index a code folder so coding agents (Claude Code, GitHub Copilot, anything that reads
`AGENTS.md` or speaks MCP) get better context. Fully offline: keywords and symbols only, no
embeddings and no network.

ctxindex gives agents context in two ways:

1. **Context files.** It writes a compact, auto-generated block into `AGENTS.md`, `CLAUDE.md`
   and `.github/copilot-instructions.md`, between `<!-- ctxindex:start -->` and
   `<!-- ctxindex:end -->`. The block lists languages and file counts, a depth-limited directory
   tree, entry points, one-line summaries of the key files, and a note on which MCP tools to use.
   **Text outside the markers is never touched.** The full map goes to `.ctxindex/PROJECT_MAP.md`.
2. **A local MCP server (stdio).** It exposes `project_overview`, `search_code`, `find_symbol`,
   `file_outline`, `read_file` and `reindex`. Before each query it runs an incremental reindex,
   throttled to at most once every 10 s by default, so results stay fresh while you edit.

## Install

```bash
pip install -e .          # from this folder; gives you the `ctxindex` command
# or, for development:
pip install -e '.[dev]' && pytest
```

Requires Python ≥ 3.10. Dependencies: `mcp` (SDK 2.x `MCPServer` or 1.x `FastMCP`, both
supported) and `pathspec`.

## Quick start

```bash
cd my-project
ctxindex init            # index + context files + .mcp.json + .vscode/mcp.json
```

Then open the project in Claude Code (it picks up `.mcp.json`) or in VS Code with Copilot agent
mode (it picks up `.vscode/mcp.json`) and approve the `ctxindex` server.

| Command | What it does |
|---|---|
| `ctxindex init [path]` | Index, write the context blocks and PROJECT_MAP, and write/merge `.mcp.json` (Claude Code) and `.vscode/mcp.json` (Copilot). Flags: `--no-context`, `--no-mcp`, `--max-files N`, `--depth N`, `--full` |
| `ctxindex index [path]` | Incremental (re)index. `--full` re-parses everything |
| `ctxindex context [path]` | Regenerate the context blocks and PROJECT_MAP. `--stdout` prints the block instead |
| `ctxindex search QUERY… [-g GLOB] [-l LANG] [-n N]` | Keyword search, e.g. `ctxindex search retry backoff -g 'src/**/*.ts'` |
| `ctxindex symbol NAME [-k KIND]` | Find definitions, e.g. `ctxindex symbol UserService.get_user`, `-k class` |
| `ctxindex outline FILE` | Symbol outline of one file |
| `ctxindex serve [--root DIR] [--refresh-interval S]` | Run the MCP server on stdio |

Commands that take `--root` default to the nearest parent folder that contains `.ctxindex/`,
then the nearest one that contains `.git/`.

Re-running `ctxindex context` on an unchanged tree changes no files, so the context files don't
churn in git. Commit `AGENTS.md` and the other context files if you like. `.ctxindex/` gets its
own `.gitignore`, which keeps `index.db` out of git.

## MCP tools

| Tool | Purpose |
|---|---|
| `project_overview()` | Languages, layout, entry points and key-file summaries. Call it first |
| `search_code(query, path_glob, lang, limit)` | BM25 full-text search over ~40-line code chunks. Identifiers match by prefix and by camelCase/snake_case parts. Hits for an identifier rank its definition above its usages, and code above prose. Returns `path:start-end`, the enclosing symbol and the matching lines |
| `find_symbol(name, kind)` | Looks for an exact name first, then prefix, substring and fuzzy (identifier parts) matches. Accepts `Class.method`. `kind="function"` also matches methods, and `kind="type"` matches classes, interfaces, structs, records and enums |
| `file_outline(path)` | Nested symbols with line ranges and docstrings. A unique path suffix such as `models.py` works |
| `read_file(path, start_line, end_line)` | Numbered lines, capped at 1000 per call. Paths outside the project root are refused |
| `reindex(full)` | Force a refresh, e.g. after switching branches |

## What gets indexed

| Language | Extracted |
|---|---|
| Python | classes, functions, methods, properties, top-level constants, docstrings, module docstring (via `ast`; regex fallback for files that don't parse) |
| JavaScript / TypeScript (`.js .jsx .mjs .cjs .ts .tsx .mts .cts`) | functions, arrow functions, React components, classes, methods, interfaces, types, enums, namespaces, top-level constants, JSDoc summaries |
| C# / .NET | namespaces (block and file-scoped), classes, interfaces, records, structs, enums, delegates, methods, constructors, properties, `///` summaries. `.csproj` and `.sln` files are detected as entry points |
| HTML | `<title>`, element ids, headings, `<script src>` and stylesheet links, plus symbols from inline `<script>` and `<style>` blocks |
| CSS / SCSS / Less | selectors (including nested ones), at-rules, `@mixin`, `@function`, `@keyframes`, custom properties, top-level `$variables` |
| Markdown | heading sections (ATX and setext) with nesting, ignoring headings inside fenced code |

Other text files (JSON, YAML, shell, SQL, …) are still keyword-searchable. Entry points are
detected heuristically: `__main__` guards, `Program.cs` and `Main()`, `index.html`,
`src/index.*`, `package.json` main/bin/scripts, `pyproject.toml` console scripts, and similar.

**Skipped:** anything matched by `.gitignore` (nested files supported) or `.ctxindexignore`;
`node_modules`, `bin`, `obj`, `.git`, `dist`, `build`, `.venv`, `__pycache__` and similar folders;
any virtualenv; lockfiles; binaries (by extension or NUL bytes); minified files; and files over
1 MB (`--max-file-size`).

**Storage:** SQLite at `.ctxindex/index.db`. Tables: `files`, `symbols` (name, kind, path, line,
end_line, signature, parent, docstring), `chunks`, plus FTS5 tables over code chunks and over
symbols. Updates are incremental. A file is skipped when its mtime and size are unchanged, and
re-parsed only when its content hash has changed.

## Adding a language

Extractors are pluggable. Create `ctxindex/extractors/<lang>.py`:

```python
from .base import Extractor, FileInfo, Symbol, register

class GoExtractor(Extractor):
    language = "go"
    extensions = (".go",)

    def extract(self, text: str, path: str) -> FileInfo:
        info = FileInfo()
        ...  # append Symbol(name=..., kind=..., line=..., end_line=..., parent=...)
        return info

register(GoExtractor())
```

Then import the module in `ctxindex/extractors/__init__.py`. `base.py` has helpers for C-like
languages: `strip_c_like` (blanks comments and strings), `match_braces`, `block_end` and
`assign_parents`.

## Performance

These numbers come from a 1.35M-line Python tree (pandas, numpy, pydantic, and others; 3.5k
files):

| Operation | Time |
|---|---|
| Full index | ~17 s |
| No-op incremental refresh | ~0.05 s |
| CLI query, including Python startup | ~0.15 s |
