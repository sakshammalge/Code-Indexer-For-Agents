"""Command-line interface: ctxindex init | index | context | search | symbol | outline | serve."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, tools
from .context import CONTEXT_FILES, write_context, write_mcp_configs
from .index import Index, find_root
from .scanner import DEFAULT_MAX_BYTES


def _root(args) -> Path:
    given = getattr(args, "path", None) or getattr(args, "root", None)
    if given:
        p = Path(given).resolve()
        if not p.is_dir():
            sys.exit(f"ctxindex: not a directory: {given}")
        return p
    return find_root()


def _open(args) -> Index:
    return Index(_root(args), max_bytes=getattr(args, "max_file_size", DEFAULT_MAX_BYTES))


def _print_results(results: dict[str, str]) -> None:
    for path, status in results.items():
        print(f"  {status:<10} {path}")


def cmd_init(args) -> int:
    with _open(args) as idx:
        st = idx.update(full=args.full)
        print(f"Indexed {idx.root}: {st}")
        if not args.no_mcp:
            print("MCP configs:")
            _print_results(write_mcp_configs(idx.root))
        if not args.no_context:
            print("Context files:")
            _print_results(write_context(idx, max_files=args.max_files, tree_depth=args.depth))
    return 0


def cmd_index(args) -> int:
    with _open(args) as idx:
        st = idx.update(full=args.full)
        s = idx.stats()
        print(f"Indexed {idx.root}: {st}")
        print(f"  {s['files']} files, {s['symbols']} symbols, {s['lines']} lines -> {idx.db_path}")
    return 0


def cmd_context(args) -> int:
    with _open(args) as idx:
        if idx.is_empty() or not args.no_refresh:
            idx.update()
        if args.stdout:
            from .context import render_block
            print(render_block(idx, max_files=args.max_files, tree_depth=args.depth))
            return 0
        _print_results(write_context(idx, max_files=args.max_files, tree_depth=args.depth))
    return 0


def _query_index(args) -> Index:
    idx = _open(args)
    idx.update()  # incremental: cheap when nothing changed
    return idx


def cmd_search(args) -> int:
    with _query_index(args) as idx:
        print(tools.search_code(idx, " ".join(args.query), args.glob, args.lang, args.limit))
    return 0


def cmd_symbol(args) -> int:
    with _query_index(args) as idx:
        print(tools.find_symbol(idx, args.name, args.kind, args.limit))
    return 0


def cmd_outline(args) -> int:
    with _query_index(args) as idx:
        print(tools.file_outline(idx, args.file))
    return 0


def cmd_serve(args) -> int:
    from .server import serve  # imports the mcp SDK only when needed

    serve(_root(args), refresh_interval=args.refresh_interval)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctxindex", description="Index a codebase for coding agents "
                                "(context files + local MCP server). Fully offline.")
    p.add_argument("--version", action="version", version=f"ctxindex {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def common(sp, positional_path: bool = False):
        if positional_path:
            sp.add_argument("path", nargs="?", help="project folder (default: current directory)")
        else:
            sp.add_argument("--root", "-C", help="project root (default: nearest folder with .ctxindex/ or .git/)")
        sp.add_argument("--max-file-size", type=int, default=DEFAULT_MAX_BYTES, metavar="BYTES",
                        help=f"skip files larger than this (default {DEFAULT_MAX_BYTES})")

    def ctx_opts(sp):
        sp.add_argument("--max-files", type=int, default=40, help="key files listed in the context block (default 40)")
        sp.add_argument("--depth", type=int, default=2, help="directory tree depth in the context block (default 2)")

    sp = sub.add_parser("init", help="index + write context files + write MCP configs")
    common(sp, positional_path=True)
    ctx_opts(sp)
    sp.add_argument("--full", action="store_true", help="re-parse every file")
    sp.add_argument("--no-context", action="store_true", help=f"don't write {', '.join(CONTEXT_FILES)}")
    sp.add_argument("--no-mcp", action="store_true", help="don't write .mcp.json / .vscode/mcp.json")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("index", help="(re)build the index incrementally")
    common(sp, positional_path=True)
    sp.add_argument("--full", action="store_true", help="re-parse every file")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("context", help="regenerate AGENTS.md / CLAUDE.md / copilot-instructions blocks")
    common(sp, positional_path=True)
    ctx_opts(sp)
    sp.add_argument("--stdout", action="store_true", help="print the block instead of writing files")
    sp.add_argument("--no-refresh", action="store_true", help="don't reindex first")
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("search", help="keyword search over code")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--glob", "-g", help="path glob, e.g. 'src/**/*.ts'")
    sp.add_argument("--lang", "-l", help="language filter, e.g. python, ts, csharp")
    sp.add_argument("--limit", "-n", type=int, default=10)
    common(sp)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("symbol", help="find symbol definitions")
    sp.add_argument("name")
    sp.add_argument("--kind", "-k", help="class, function, method, interface, selector, ...")
    sp.add_argument("--limit", "-n", type=int, default=30)
    common(sp)
    sp.set_defaults(func=cmd_symbol)

    sp = sub.add_parser("outline", help="show the symbol outline of a file")
    sp.add_argument("file")
    common(sp)
    sp.set_defaults(func=cmd_outline)

    sp = sub.add_parser("serve", help="run the MCP server on stdio")
    common(sp)
    sp.add_argument("--refresh-interval", type=float, default=10.0, metavar="SECONDS",
                    help="minimum seconds between automatic incremental reindexes (default 10)")
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # e.g. `ctxindex search foo | head`
        sys.stderr.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
