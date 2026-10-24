"""SQLite-backed index: schema, incremental (re)indexing and keyword/symbol queries."""

from __future__ import annotations

import functools
import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import entrypoints
from .extractors import FileInfo, detect_language, get_extractor
from .scanner import DEFAULT_MAX_BYTES, is_binary, scan

SCHEMA_VERSION = "1"
INDEX_DIR = ".ctxindex"
DB_NAME = "index.db"
MARK_START = "<!-- ctxindex:start -->"
MARK_END = "<!-- ctxindex:end -->"
CHUNK_LINES = 40
CHUNK_SLACK = 10
MAX_CHUNK_CHARS = 16_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    lang TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    hash TEXT,
    lines INTEGER,
    doc TEXT,
    entry TEXT,
    indexed_at REAL
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER,
    signature TEXT,
    parent TEXT,
    docstring TEXT
);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_id);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, idents, path);
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(name, name_parts, signature, docstring, path);
"""

LANG_ALIASES = {
    "py": "python", "js": "javascript", "jsx": "javascript", "ts": "typescript",
    "tsx": "typescript", "cs": "csharp", "c#": "csharp", "dotnet": "csharp", ".net": "csharp",
    "md": "markdown", "htm": "html", "sass": "scss", "less": "scss",
}
KIND_ALIASES = {
    "function": ("function", "method", "constructor", "component"),
    "func": ("function", "method", "constructor", "component"),
    "fn": ("function", "method", "constructor", "component"),
    "def": ("function", "method", "constructor", "component"),
    "type": ("class", "interface", "struct", "record", "enum", "type", "delegate"),
    "css": ("selector", "variable", "mixin", "keyframes", "at-rule"),
    "heading": ("heading", "section"),
}
_KIND_RANK = {k: i for i, k in enumerate([
    "class", "interface", "record", "struct", "enum", "type", "component", "function", "method",
    "constructor", "property", "namespace", "delegate", "constant", "variable", "mixin",
    "selector", "section", "heading", "id", "title", "keyframes", "at-rule", "script",
    "stylesheet", "link",
])}

_PROSE_LANGS = {"markdown", "text", "rst"}
_WORD = re.compile(r"[A-Za-z0-9_$]+")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_HUMP = re.compile(r"[a-z0-9][A-Z]|[A-Z][A-Z][a-z]")


@functools.lru_cache(maxsize=65536)
def _camel_parts(ident: str) -> tuple[str, ...]:
    return tuple(split_ident(ident))


def split_ident(name: str) -> list[str]:
    """getUserById -> [get, user, by, id]; HTTPServer_v2 -> [http, server, v, 2]."""
    parts: list[str] = []
    for piece in re.split(r"[_$\-.#\s]+", name):
        parts.extend(p.lower() for p in _CAMEL_PART.findall(piece))
    return parts


_BLOCK_RE = re.compile(r"^" + re.escape(MARK_START) + r"[ \t]*\r?$.*?^" + re.escape(MARK_END) + r"[ \t]*$",
                       re.M | re.S)


def find_managed_block(text: str) -> tuple[int, int] | None:
    """(start, end) offsets of our generated block. Markers only count on lines of their own, so
    a marker quoted in prose or code is never mistaken for the block."""
    m = _BLOCK_RE.search(text)
    return (m.start(), m.end()) if m else None


def blank_managed_block(text: str) -> str:
    """Replace our own generated block with blank lines so it never pollutes search results."""
    span = find_managed_block(text)
    if span is None:
        return text
    start, end = span
    return text[:start] + "\n" * text.count("\n", start, end) + text[end:]


def find_root(start: str | Path | None = None) -> Path:
    """Walk up from ``start`` looking for an existing index, then a VCS root; else ``start``."""
    here = Path(start or os.getcwd()).resolve()
    for marker in (INDEX_DIR, ".git"):
        for d in (here, *here.parents):
            if (d / marker).is_dir():
                return d
    return here


def _glob_to_regex(pattern: str) -> re.Pattern:
    pattern = pattern.strip().removeprefix("./")
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "{":
            j = pattern.find("}", i)
            if j > i:
                out.append("(?:" + "|".join(re.escape(x) for x in pattern[i + 1:j].split(",")) + ")")
                i = j + 1
                continue
            out.append(re.escape(c))
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    if "/" not in pattern:  # "*.py" matches at any depth, like .gitignore
        return re.compile(r"(?:.*/)?" + body + r"$", re.I)
    if pattern.endswith("/"):
        return re.compile(body + ".*$", re.I)
    return re.compile(body + r"(?:/.*)?$", re.I)  # "src/api" matches everything below it


_GLOB_CACHE: dict[str, re.Pattern] = {}


def glob_match(pattern: str, path: str) -> bool:
    rx = _GLOB_CACHE.get(pattern)
    if rx is None:
        rx = _GLOB_CACHE[pattern] = _glob_to_regex(pattern)
    return bool(rx.match(path))


def _chunks(lines: list[str]):
    n, i = len(lines), 0
    while i < n:
        end = min(n, i + CHUNK_LINES)
        if end < n:
            for j in list(range(end, min(n, end + CHUNK_SLACK))) + list(range(end - 1, end - CHUNK_SLACK, -1)):
                if not lines[j].strip():
                    end = j + 1
                    break
        yield i, end
        i = end


@dataclass
class UpdateStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: int = 0
    seconds: float = 0.0
    changed_paths: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def __str__(self) -> str:
        return (f"{self.added} added, {self.updated} updated, {self.removed} removed, "
                f"{self.unchanged} unchanged, {self.skipped} skipped in {self.seconds:.2f}s")


class Index:
    def __init__(self, root: str | Path, max_bytes: int = DEFAULT_MAX_BYTES):
        self.root = Path(root).resolve()
        self.dir = self.root / INDEX_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            gi.write_text("# generated by ctxindex: keep the database out of git\nindex.db*\n", encoding="utf-8")
        self.db_path = self.dir / DB_NAME
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self._last_refresh = 0.0
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.create_function("glob_match", 2, glob_match, deterministic=True)
        self._init_schema()

    # ------------------------------------------------------------------ schema / lifecycle

    def _init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(_SCHEMA)
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                if row is not None:  # incompatible older index: rebuild from scratch
                    for t in ("chunks_fts", "symbols_fts", "chunks", "symbols", "files", "meta"):
                        self.conn.execute(f"DROP TABLE IF EXISTS {t}")
                    self.conn.executescript(_SCHEMA)
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
                self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT 1 FROM files LIMIT 1").fetchone() is None

    # ------------------------------------------------------------------ indexing

    def update(self, full: bool = False) -> UpdateStats:
        """Incrementally sync the index with the file system (mtime/size, then content hash)."""
        t0 = time.perf_counter()
        st = UpdateStats()
        with self.lock:
            existing = {r["path"]: r for r in self.conn.execute(
                "SELECT id, path, size, mtime_ns, hash FROM files")}
            seen: set[str] = set()
            cur = self.conn.cursor()
            try:
                for f in scan(self.root, self.max_bytes):
                    seen.add(f.path)
                    row = existing.get(f.path)
                    if row is not None and not full and row["size"] == f.size and row["mtime_ns"] == f.mtime_ns:
                        st.unchanged += 1
                        continue
                    try:
                        with open(f.abspath, "rb") as fh:
                            data = fh.read()
                    except OSError:
                        st.skipped += 1
                        seen.discard(f.path)
                        continue
                    if is_binary(data):
                        st.skipped += 1
                        seen.discard(f.path)
                        continue
                    digest = hashlib.sha1(data).hexdigest()
                    if row is not None and not full and row["hash"] == digest:
                        cur.execute("UPDATE files SET size=?, mtime_ns=? WHERE id=?", (f.size, f.mtime_ns, row["id"]))
                        st.unchanged += 1
                        continue
                    self._index_file(cur, f.path, data, f.size, f.mtime_ns, digest,
                                     row["id"] if row is not None else None)
                    st.changed_paths.append(f.path)
                    if row is None:
                        st.added += 1
                    else:
                        st.updated += 1
                for path, row in existing.items():
                    if path not in seen:
                        self._delete_file(cur, row["id"])
                        st.removed += 1
                        st.changed_paths.append(path)
                cur.execute("INSERT OR REPLACE INTO meta VALUES ('last_indexed', ?)", (str(time.time()),))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            self._last_refresh = time.monotonic()
        st.seconds = time.perf_counter() - t0
        return st

    def maybe_refresh(self, interval: float) -> UpdateStats | None:
        """Throttled incremental reindex: at most once every ``interval`` seconds."""
        if time.monotonic() - self._last_refresh < interval:
            return None
        return self.update()

    def _delete_file(self, cur: sqlite3.Cursor, file_id: int, keep_row: bool = False) -> None:
        cur.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (file_id,))
        cur.execute("DELETE FROM symbols_fts WHERE rowid IN (SELECT id FROM symbols WHERE file_id=?)", (file_id,))
        cur.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        cur.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
        if not keep_row:
            cur.execute("DELETE FROM files WHERE id=?", (file_id,))

    def _index_file(self, cur, path: str, data: bytes, size: int, mtime_ns: int, digest: str,
                    file_id: int | None) -> None:
        text = data.decode("utf-8", errors="replace").lstrip("﻿")
        if MARK_START in text and path.lower().endswith((".md", ".markdown")):
            text = blank_managed_block(text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        lang = detect_language(path)
        minified = len(lines) < 10 and size > 5000 or (lines and max(map(len, lines)) > 5000)
        info = FileInfo()
        ex = get_extractor(path)
        if ex and not minified:
            try:
                info = ex.extract(text, path)
            except Exception:  # never let one odd file break indexing
                info = FileInfo()
        entry = entrypoints.detect(path, text)
        doc = "(minified/generated; content not indexed)" if minified else info.doc
        now = time.time()
        if file_id is None:
            cur.execute(
                "INSERT INTO files(path, lang, size, mtime_ns, hash, lines, doc, entry, indexed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (path, lang, size, mtime_ns, digest, len(lines), doc, entry, now))
            file_id = cur.lastrowid
        else:
            self._delete_file(cur, file_id, keep_row=True)
            cur.execute("UPDATE files SET lang=?, size=?, mtime_ns=?, hash=?, lines=?, doc=?, entry=?,"
                        " indexed_at=? WHERE id=?",
                        (lang, size, mtime_ns, digest, len(lines), doc, entry, now, file_id))
        for s in info.symbols:
            cur.execute(
                "INSERT INTO symbols(file_id, name, kind, path, line, end_line, signature, parent, docstring)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (file_id, s.name, s.kind, path, s.line, s.end_line or s.line, s.signature, s.parent, s.docstring))
            cur.execute("INSERT INTO symbols_fts(rowid, name, name_parts, signature, docstring, path)"
                        " VALUES (?,?,?,?,?,?)",
                        (cur.lastrowid, s.name, " ".join(split_ident(s.name)), s.signature, s.docstring, path))
        if minified:
            return
        path_terms = " ".join(split_ident(path))
        for a, b in _chunks(lines):
            content = "\n".join(lines[a:b])
            if not content.strip():
                continue
            # FTS already splits snake_case; add camelCase parts so "user" finds getUserById
            idents = {p for ident in set(_IDENT.findall(content)) if _HUMP.search(ident)
                      for p in _camel_parts(ident)}
            cur.execute("INSERT INTO chunks(file_id, start_line, end_line) VALUES (?,?,?)", (file_id, a + 1, b))
            cur.execute("INSERT INTO chunks_fts(rowid, content, idents, path) VALUES (?,?,?,?)",
                        (cur.lastrowid, content[:MAX_CHUNK_CHARS], " ".join(sorted(idents)), path_terms))

    # ------------------------------------------------------------------ path helpers

    def resolve_path(self, path: str) -> str | None:
        """Normalise a user/agent supplied path to an indexed project-relative path."""
        p = path.strip().replace("\\", "/")
        if os.path.isabs(p):
            try:
                p = Path(p).resolve().relative_to(self.root).as_posix()
            except ValueError:
                return None
        while p.startswith("./"):
            p = p[2:]
        row = self.conn.execute("SELECT path FROM files WHERE path=?", (p,)).fetchone()
        if row:
            return row[0]
        rows = self.conn.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' LIMIT 5",
                                 ("%/" + p.replace("%", "\\%").replace("_", "\\_"),)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        return None

    def safe_abspath(self, path: str) -> Path | None:
        """Absolute path for ``path`` if it lies inside the project root (symlinks resolved)."""
        p = path.strip().replace("\\", "/")
        cand = Path(p) if os.path.isabs(p) else self.root / p
        try:
            real = cand.resolve()
            real.relative_to(self.root)
        except (ValueError, OSError):
            return None
        return real

    # ------------------------------------------------------------------ queries

    def stats(self) -> dict:
        q = self.conn.execute
        langs = [dict(r) for r in q(
            "SELECT lang, COUNT(*) AS files, SUM(lines) AS lines FROM files GROUP BY lang ORDER BY files DESC, lang")]
        last = q("SELECT value FROM meta WHERE key='last_indexed'").fetchone()
        return {
            "files": q("SELECT COUNT(*) FROM files").fetchone()[0],
            "symbols": q("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "lines": q("SELECT COALESCE(SUM(lines), 0) FROM files").fetchone()[0],
            "languages": langs,
            "last_indexed": float(last[0]) if last else None,
        }

    def files(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM files ORDER BY path").fetchall()

    def symbols_for(self, path: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM symbols WHERE path=? ORDER BY line, end_line DESC", (path,)).fetchall()

    def all_symbols(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM symbols ORDER BY path, line").fetchall()

    def file_row(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()

    @staticmethod
    def _fts_terms(query: str) -> list[str]:
        """Split a free-text query into FTS5-safe terms (quoted phrases are kept together)."""
        terms: list[str] = []
        for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query):
            if phrase:
                words = _WORD.findall(phrase)
                if words:
                    terms.append('"' + " ".join(words) + '"')
                continue
            for w in _WORD.findall(word):
                w = w.strip("$")
                if not w:
                    continue
                # snake_case / dotted pieces become an adjacent phrase; allow prefix matches
                terms.append(f'"{w}"*' if len(w) >= 3 else f'"{w}"')
        return terms

    def search(self, query: str, path_glob: str | None = None, lang: str | None = None,
               limit: int = 10, max_lines: int = 6) -> list[dict]:
        terms = self._fts_terms(query)
        if not terms:
            return []
        limit = max(1, min(int(limit or 10), 100))
        lang = LANG_ALIASES.get((lang or "").lower(), (lang or "").lower()) or None
        rows: list[sqlite3.Row] = []
        exprs = [" AND ".join(terms)] + ([" OR ".join(terms)] if len(terms) > 1 else [])
        with self.lock:
            for expr in exprs:
                sql = ("SELECT c.id, c.start_line, c.end_line, f.path, f.lang, chunks_fts.content AS content,"
                       " bm25(chunks_fts, 1.0, 0.5, 2.0) AS score"
                       " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN files f ON f.id = c.file_id"
                       " WHERE chunks_fts MATCH ?")
                params: list = [expr]
                if lang:
                    sql += " AND f.lang = ?"
                    params.append(lang)
                if path_glob:
                    sql += " AND glob_match(?, f.path)"
                    params.append(path_glob)
                sql += " ORDER BY score LIMIT ?"
                params.append(limit * 5)
                try:
                    rows = self.conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    break
            if not rows:
                return []
            words = [w.lower() for t in terms for w in _WORD.findall(t)]
            defs = {}
            qmarks = ",".join("?" * len(words))
            for r in self.conn.execute(
                    f"SELECT path, line FROM symbols WHERE lower(name) IN ({qmarks})", words):
                defs.setdefault(r["path"], []).append(r["line"])

            # identifier-looking queries (getUser, get_user, or a known symbol) favour code over prose
            identish = bool(defs) or any(_HUMP.search(w) or "_" in w for w in _WORD.findall(query))
            scored = []
            for r in rows:
                score = r["score"]  # bm25: more negative is better
                if any(r["start_line"] <= ln <= r["end_line"] for ln in defs.get(r["path"], ())):
                    score *= 2.5  # chunk contains the definition of a queried symbol
                if identish and r["lang"] in _PROSE_LANGS:
                    score *= 0.4
                scored.append((score, r))
            scored.sort(key=lambda x: x[0])
            per_file: dict[str, int] = {}
            results = []
            for score, r in scored:
                if per_file.get(r["path"], 0) >= 3:
                    continue
                per_file[r["path"]] = per_file.get(r["path"], 0) + 1
                shown = _matching_lines(r["content"], r["start_line"], words, max_lines)
                results.append({
                    "path": r["path"], "lang": r["lang"], "start_line": r["start_line"],
                    "end_line": r["end_line"], "score": round(-score, 3),
                    "symbol": self._enclosing(r["path"], shown[0][0] if shown else r["start_line"]),
                    "lines": shown,
                })
                if len(results) >= limit:
                    break
        return results

    def _enclosing(self, path: str, line: int) -> str | None:
        """Innermost symbol containing ``line``."""
        first = line
        row = self.conn.execute(
            "SELECT name, kind, parent FROM symbols WHERE path=? AND line<=? AND end_line>=?"
            " AND kind NOT IN ('namespace','at-rule','id','heading','title','variable','constant',"
            "'script','stylesheet','link')"
            " ORDER BY line DESC LIMIT 1", (path, first, first)).fetchone()
        if not row:
            return None
        name = f"{row['parent']}.{row['name']}" if row["parent"] and row["kind"] != "section" else row["name"]
        return f"{row['kind']} {name}"

    def find_symbol(self, name: str, kind: str | None = None, limit: int = 30) -> list[sqlite3.Row]:
        name = name.strip()
        if not name:
            return []
        limit = max(1, min(int(limit or 30), 200))
        parent = None
        if "." in name and not name.startswith((".", "#")) and "/" not in name and " " not in name:
            parent, name = name.rsplit(".", 1)
        elif "::" in name:
            parent, name = name.rsplit("::", 1)
        kinds: tuple[str, ...] = ()
        if kind:
            k = kind.lower().strip()
            kinds = KIND_ALIASES.get(k, (k,))
        where_kind = f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else ""

        def run(cond: str, args: list) -> list[sqlite3.Row]:
            sql = f"SELECT * FROM symbols WHERE {cond}{where_kind}"
            a = list(args) + list(kinds)
            if parent:
                sql += " AND (parent = ? COLLATE NOCASE OR parent LIKE ? ESCAPE '\\')"
                a += [parent, "%." + parent.replace("%", "\\%").replace("_", "\\_")]
            sql += " LIMIT ?"
            return self.conn.execute(sql, a + [limit * 4]).fetchall()

        esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        out: list[sqlite3.Row] = []
        seen: set[int] = set()
        with self.lock:
            stages = [
                ("name = ? COLLATE NOCASE", [name]),
                ("name LIKE ? ESCAPE '\\'", [esc + "%"]),
                ("name LIKE ? ESCAPE '\\'", ["%" + esc + "%"]),
            ]
            for stage, (cond, args) in enumerate(stages):
                rows = run(cond, args)
                rows.sort(key=lambda r: (r["name"] != name, _KIND_RANK.get(r["kind"], 99),
                                         len(r["name"]), r["path"].count("/"), r["path"], r["line"]))
                for r in rows:
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        out.append(r)
                if len(out) >= limit or (stage == 0 and out):
                    break
            if not out:  # fuzzy: identifier parts via FTS (e.g. "user repo" -> UserRepository)
                terms = [f'"{p}"*' for p in split_ident(name)]
                if terms:
                    try:
                        ids = [r[0] for r in self.conn.execute(
                            "SELECT rowid FROM symbols_fts WHERE symbols_fts MATCH ? ORDER BY bm25(symbols_fts, 5.0, 3.0, 1.0, 0.5, 0.2) LIMIT ?",
                            ("{name name_parts}: (" + " AND ".join(terms) + ")", limit * 4))]
                    except sqlite3.OperationalError:
                        ids = []
                    if ids:
                        rows = self.conn.execute(
                            f"SELECT * FROM symbols WHERE id IN ({','.join('?' * len(ids))}){where_kind}",
                            ids + list(kinds)).fetchall()
                        order = {i: n for n, i in enumerate(ids)}
                        out = sorted(rows, key=lambda r: order[r["id"]])
        return out[:limit]


def _matching_lines(content: str, start_line: int, words: list[str], max_lines: int) -> list[tuple[int, str]]:
    lines = content.split("\n")
    hits = []
    for i, line in enumerate(lines):
        low = line.lower()
        n = sum(1 for w in set(words) if w in low)
        if n:
            hits.append((n, i))
    if not hits:
        return [(start_line + i, lines[i][:240]) for i in range(min(3, len(lines)))]
    best = sorted(sorted(hits, key=lambda h: (-h[0], h[1]))[:max_lines], key=lambda h: h[1])
    return [(start_line + i, lines[i].rstrip()[:240]) for _, i in best]
