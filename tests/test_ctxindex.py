import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from ctxindex import tools
from ctxindex.cli import main
from ctxindex.context import upsert_block, write_context
from ctxindex.extractors import get_extractor
from ctxindex.index import MARK_END, MARK_START, Index, glob_match, split_ident

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "sample"
    shutil.copytree(FIXTURE, root)
    # junk that must never be indexed (created here so the repo's own .gitignore can't drop it)
    for rel, content in {
        "node_modules/lib/index.js": "function nodeModulesOnly() {}",
        "build/out.py": "def build_output_only(): pass",
        "Api/bin/Debug/x.cs": "class BinOnly {}",
        "Api/obj/y.cs": "class ObjOnly {}",
        "generated/gen.py": "GITIGNORED_ONLY = 1",
        "debug.log": "log_only",
        ".venv/lib/site.py": "def venv_only(): pass",
        "web/vendor.min.js": "function minifiedOnly(){}",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (root / "web" / "logo.dat").write_bytes(b"\x89PNG\x00\x00binary_only")
    (root / "big.txt").write_text("huge_only " * 200_000)
    return root


@pytest.fixture()
def index(repo):
    idx = Index(repo)
    idx.update()
    yield idx
    idx.close()


def paths(idx):
    return {f["path"] for f in idx.files()}


def test_scan_respects_ignores(index):
    p = paths(index)
    assert "app/models.py" in p and "web/src/components/ItemList.tsx" in p
    for bad in ("node_modules/lib/index.js", "build/out.py", "Api/bin/Debug/x.cs", "Api/obj/y.cs",
                "generated/gen.py", "debug.log", ".venv/lib/site.py", "web/vendor.min.js",
                "web/logo.dat", "big.txt"):
        assert bad not in p, bad
    assert not any(x.startswith(".ctxindex") for x in p)


def test_python_symbols(index):
    syms = {(s["name"], s["kind"], s["parent"]) for s in index.symbols_for("app/models.py")}
    assert ("Item", "class", None) in syms
    assert ("restock", "method", "Item") in syms
    assert ("in_stock", "property", "Item") in syms
    assert ("fetch_remote_inventory", "method", "Warehouse") in syms
    assert ("MAX_ITEMS", "constant", None) in syms
    item = next(s for s in index.symbols_for("app/models.py") if s["name"] == "Item")
    assert (item["line"], item["end_line"]) == (8, 19)
    assert item["docstring"] == "A single stock-keeping unit."


def test_ts_and_tsx_symbols(index):
    syms = {(s["name"], s["kind"]) for s in index.symbols_for("web/src/api.ts")}
    assert {("InventoryItem", "interface"), ("Sku", "type"), ("StockLevel", "enum"),
            ("fetchItems", "function"), ("InventoryClient", "class"), ("getItem", "method"),
            ("formatSku", "function"), ("API_BASE", "variable")} <= syms
    get_item = next(s for s in index.symbols_for("web/src/api.ts") if s["name"] == "getItem")
    assert get_item["parent"] == "InventoryClient" and get_item["docstring"] == "Look up a single item by SKU."
    tsx = {(s["name"], s["kind"]) for s in index.symbols_for("web/src/components/ItemList.tsx")}
    assert ("ItemList", "component") in tsx and ("ItemBadge", "component") in tsx


def test_csharp_symbols(index):
    rows = index.symbols_for("Api/Services/InventoryService.cs")
    syms = {(s["name"], s["kind"], s["parent"]) for s in rows}
    assert ("Api.Services", "namespace", None) in syms
    assert ("IInventoryService", "interface", "Api.Services") in syms
    assert ("ItemDto", "record", "Api.Services") in syms
    assert ("Warehouse", "enum", "Api.Services") in syms
    assert ("InventoryService", "constructor", "InventoryService") in syms
    assert ("GetAll", "method", "InventoryService") in syms
    assert ("CountRemoteAsync", "method", "InventoryService") in syms  # multi-line params + where
    assert ("Count", "property", "InventoryService") in syms
    assert ("Region", "property", "InventoryService") in syms
    assert ("Stats", "struct", "InventoryService") in syms
    get_all = next(s for s in rows if s["name"] == "GetAll" and s["parent"] == "InventoryService")
    assert get_all["docstring"] == "Returns every item." and get_all["end_line"] == 37
    assert not any(s["name"] in ("if", "foreach", "TryGetValue") for s in rows)


def test_html_css_markdown(index):
    html = {(s["name"], s["kind"]) for s in index.symbols_for("web/index.html")}
    assert {("Inventory Dashboard", "title"), ("#root", "id"), ("#reports", "id"),
            ("Inventory", "heading"), ("Reports", "heading"), ("src/index.js", "script"),
            ("styles/main.css", "stylesheet"), ("trackPageView", "function"), (".hero", "selector")} <= html
    track = next(s for s in index.symbols_for("web/index.html") if s["name"] == "trackPageView")
    assert track["line"] == 16  # inline script line numbers are file-relative

    css = {(s["name"], s["kind"]) for s in index.symbols_for("web/styles/main.scss")}
    assert {("--spacing-unit", "variable"), ("--color-primary", "variable"), ("$brand-color", "variable"),
            ("card-shadow", "mixin"), (".item-list", "selector"), ("li", "selector"),
            ("pulse", "keyframes")} <= css
    assert not any("#{" in n and k == "selector" for n, k in css)

    md = [(s["name"], s["parent"]) for s in index.symbols_for("docs/architecture.md")]
    assert ("Architecture", None) in md and ("Data model", "Backend") in md
    assert ("not a heading", "Backend") not in md and not any("not a heading" in n for n, _ in md)
    assert ("Setext Heading", "Architecture") in md


def test_entry_points(index):
    entries = {f["path"]: f["entry"] for f in index.files() if f["entry"]}
    assert "app/main.py" in entries and "Api/Program.cs" in entries
    assert "app.main:main" in entries["pyproject.toml"]
    assert "executable" in entries["Api/Api.csproj"]
    assert "main=src/index.js" in entries["web/package.json"]


def test_search(index):
    hits = index.search("fetchItems")
    assert hits[0]["path"] == "web/src/api.ts"  # the definition outranks usages
    assert any(n == 16 for n, _ in hits[0]["lines"])
    hits = index.search("remote inventory", path_glob="app/**")
    assert hits and all(h["path"].startswith("app/") for h in hits)
    assert all(h["lang"] == "csharp" for h in index.search("item", lang="cs"))
    # camelCase parts are searchable
    assert any(h["path"] == "web/src/api.ts" for h in index.search("abort signal"))
    assert index.search("GITIGNORED_ONLY") == []
    assert index.search("") == [] and index.search('"*:(') == []  # FTS syntax can't break it


def test_identifier_queries_prefer_code_over_prose(index, repo):
    (repo / "docs" / "changelog.md").write_text("# Changes\n\n" + "- fixed `fetchItems` retry; fetchItems now faster\n" * 20)
    index.update()
    assert index.search("fetchItems")[0]["path"] == "web/src/api.ts"


def test_find_symbol(index):
    rows = index.find_symbol("Warehouse")
    assert {r["path"] for r in rows} == {"app/models.py", "Api/Services/InventoryService.cs"}
    rows = index.find_symbol("InventoryService.GetAll")
    assert len(rows) == 1 and rows[0]["line"] == 33
    assert all(r["kind"] in ("class", "interface", "struct", "record", "enum", "type", "delegate")
               for r in index.find_symbol("Inventory", kind="type"))
    assert index.find_symbol("inventory client")[0]["name"] == "InventoryClient"  # fuzzy parts
    assert index.find_symbol("Warehouse.fetch_remote_inventory")[0]["path"] == "app/models.py"


def test_incremental_update(index, repo):
    st = index.update()
    assert not st.changed and st.unchanged > 0
    f = repo / "app" / "models.py"
    # touch without change: mtime differs, hash identical -> not re-parsed
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    st = index.update()
    assert not st.changed
    f.write_text(f.read_text() + "\n\ndef brand_new_function():\n    pass\n")
    st = index.update()
    assert st.updated == 1 and st.changed_paths == ["app/models.py"]
    assert index.find_symbol("brand_new_function")[0]["path"] == "app/models.py"
    (repo / "web" / "src" / "index.js").unlink()
    (repo / "app" / "extra.py").write_text("class Extra: pass\n")
    st = index.update()
    assert st.removed == 1 and st.added == 1
    assert "web/src/index.js" not in paths(index)
    assert all(h["path"] != "web/src/index.js" for h in index.search("boot"))


def test_maybe_refresh_throttles(index, repo):
    index.update()
    (repo / "app" / "late.py").write_text("def late(): pass\n")
    assert index.maybe_refresh(60) is None  # throttled
    assert index.maybe_refresh(0).added == 1


def test_upsert_block_preserves_outside_text(tmp_path):
    f = tmp_path / "AGENTS.md"
    assert upsert_block(f, f"{MARK_START}\nA\n{MARK_END}") == "created"
    f.write_text("# Mine\n\nbefore\n\n" + f.read_text() + "after text\n")
    assert upsert_block(f, f"{MARK_START}\nB\n{MARK_END}") == "updated"
    assert f.read_text() == f"# Mine\n\nbefore\n\n{MARK_START}\nB\n{MARK_END}\nafter text\n"
    assert upsert_block(f, f"{MARK_START}\nB\n{MARK_END}") == "unchanged"
    g = tmp_path / "CLAUDE.md"
    g.write_text("no markers")
    upsert_block(g, f"{MARK_START}\nC\n{MARK_END}")
    assert g.read_text() == f"no markers\n\n{MARK_START}\nC\n{MARK_END}\n"


def test_write_context_is_stable_and_not_indexed(index, repo):
    (repo / "CLAUDE.md").write_text("# House rules\n\nKeep me.\n")
    res = write_context(index)
    assert res["AGENTS.md"] == "created" and res["CLAUDE.md"] == "updated"
    assert (repo / ".github" / "copilot-instructions.md").exists()
    text = (repo / "CLAUDE.md").read_text()
    assert text.startswith("# House rules\n\nKeep me.\n") and MARK_START in text and MARK_END in text
    assert "`app/models.py`" in text and "Python" in text and "search_code" in text
    pm = (repo / ".ctxindex" / "PROJECT_MAP.md").read_text()
    assert "InventoryService" in pm and "restock" in pm
    # second run is a fixed point
    assert set(write_context(index).values()) == {"unchanged"}
    # generated block is never a search hit
    assert all(h["path"] not in ("AGENTS.md", "CLAUDE.md") for h in index.search("search_code"))


def test_tools_outline_and_read(index, repo):
    out = tools.file_outline(index, "InventoryService.cs")  # unique suffix resolves
    assert "L33-37" in out and "GetAll" in out
    out = tools.read_file(index, "app/main.py", 6, 7)
    assert out.splitlines()[1].startswith("6| def main")
    assert "outside the project root" in tools.read_file(index, "../../etc/passwd")
    assert "outside the project root" in tools.read_file(index, "/etc/passwd")


def test_glob_match():
    assert glob_match("*.py", "a/b/c.py") and not glob_match("*.py", "a/b/c.pyc")
    assert glob_match("src/**/*.ts", "src/a/b/x.ts") and glob_match("src/**/*.ts", "src/x.ts")
    assert glob_match("tests/", "tests/unit/a.py") and glob_match("web/src", "web/src/api.ts")
    assert glob_match("*.{ts,tsx}", "a/b.tsx")


def test_split_ident():
    assert split_ident("getUserById") == ["get", "user", "by", "id"]
    assert split_ident("HTTPServer_v2") == ["http", "server", "v", "2"]


def test_cli_init(repo, capsys, monkeypatch):
    assert main(["init", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "AGENTS.md" in out
    cfg = json.loads((repo / ".mcp.json").read_text())
    assert cfg["mcpServers"]["ctxindex"]["type"] == "stdio"
    vs = json.loads((repo / ".vscode" / "mcp.json").read_text())
    assert "${workspaceFolder}" in vs["servers"]["ctxindex"]["args"]
    monkeypatch.chdir(repo / "app")  # root auto-detected from a subdirectory
    assert main(["symbol", "Item"]) == 0
    assert "app/models.py:8-19" in capsys.readouterr().out
    assert main(["search", "restock"]) == 0
    assert "app/models.py" in capsys.readouterr().out
    # re-running context changes nothing
    assert main(["context", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "updated" not in out and "created" not in out


def test_mcp_server_stdio(index, repo):
    sys.path.insert(0, str(Path(__file__).parent))
    from mcp_client import StdioClient

    c = StdioClient([sys.executable, "-m", "ctxindex", "serve", "--root", str(repo),
                     "--refresh-interval", "0"])
    try:
        assert c.initialize()["serverInfo"]["name"] == "ctxindex"
        names = {t["name"] for t in c.request("tools/list")["tools"]}
        assert names == {"project_overview", "search_code", "find_symbol", "file_outline", "read_file", "reindex"}
        assert "Entry points" in c.call("project_overview")
        assert "web/src/api.ts" in c.call("search_code", query="fetchItems", lang="ts")
        assert "app/models.py:8-19" in c.call("find_symbol", name="Item", kind="class")
        assert "restock" in c.call("file_outline", path="app/models.py")
        assert "def main" in c.call("read_file", path="app/main.py", start_line=1, end_line=8)
        # freshness: a new file is visible on the next query without calling reindex
        (repo / "app" / "fresh.py").write_text("def freshly_added_symbol():\n    return 1\n")
        assert "app/fresh.py" in c.call("find_symbol", name="freshly_added_symbol")
        assert "unchanged" in c.call("reindex")
    finally:
        c.close()


def test_js_nested_functions_have_parent():
    src = "export function outer() {\n  const helper = () => {\n    return 1;\n  };\n  return helper();\n}\n"
    syms = {s.name: s.parent for s in get_extractor("a.ts").extract(src, "a.ts").symbols}
    assert syms == {"outer": None, "helper": "outer"}


@pytest.mark.parametrize("path,text,expected", [
    ("a.py", "def broken(:\n    pass\nclass Ok:\n    pass\n", {"broken", "Ok"}),
    ("a.js", "const re = /}{/g;\nfunction after() { return `${'}'}`; }\n", {"re", "after"}),
    ("a.cs", 'class A { string s = @"{ ""quoted"" }"; void M() { } }\n', {"A"}),
])
def test_extractors_are_robust(path, text, expected):
    names = {s.name for s in get_extractor(path).extract(text, path).symbols}
    assert expected <= names


def test_markers_in_code_are_not_a_block(tmp_path):
    (tmp_path / "consts.py").write_text(
        f'START = "{MARK_START}"\n\ndef keep_me():\n    pass\n\nEND = "{MARK_END}"\n')
    (tmp_path / "notes.md").write_text(f"# Notes\n\nWe use `{MARK_START}` markers.\n")
    with Index(tmp_path) as idx:
        idx.update()
        assert idx.find_symbol("keep_me")[0]["kind"] == "function"
        assert idx.search("keep_me")
        res = write_context(idx, targets=("notes.md",))
        assert res["notes.md"] == "updated"
        text = (tmp_path / "notes.md").read_text()
        assert text.startswith(f"# Notes\n\nWe use `{MARK_START}` markers.\n\n{MARK_START}\n")


def test_python_fallback_parents():
    info = get_extractor("x.py").extract("class A:\n    def m(self):\n        x = (\ndef f():\n    pass\n", "x.py")
    assert [(s.name, s.kind, s.parent) for s in info.symbols] == [
        ("A", "class", None), ("m", "method", "A"), ("f", "function", None)]


def test_regressions_from_real_repos():
    ts = "export type QueryFunction<\n  T = unknown,\n  K extends Key = Key,\n> = (ctx: K) => T\n"
    assert [s.name for s in get_extractor("a.ts").extract(ts, "a.ts").symbols] == ["QueryFunction"]
    cs = ("namespace N;\nref struct Handler\n{\n    internal static int Len(int a) => a;\n}\n"
          "static class Guard\n{\n    public static void FileExists(string path, "
          "[CallerArgumentExpression(\"path\")] string name = \"\")\n    {\n    }\n}\n")
    syms = {(s.name, s.kind, s.parent) for s in get_extractor("a.cs").extract(cs, "a.cs").symbols}
    assert {("Handler", "struct", "N"), ("Len", "method", "Handler"), ("FileExists", "method", "Guard")} <= syms
