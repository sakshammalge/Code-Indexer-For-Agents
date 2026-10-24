"""Extractor interface, registry and shared parsing helpers."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass
class Symbol:
    name: str
    kind: str
    line: int
    end_line: int | None = None
    signature: str = ""
    parent: str | None = None
    docstring: str = ""


@dataclass
class FileInfo:
    symbols: list[Symbol] = field(default_factory=list)
    doc: str = ""  # one-line description of the file (module docstring, <title>, ...)


class Extractor:
    """Base class. Subclass, set ``language`` and ``extensions``, implement ``extract``."""

    language: str = ""
    extensions: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()  # exact basenames, e.g. "Dockerfile"

    def extract(self, text: str, path: str) -> FileInfo:  # pragma: no cover - interface
        raise NotImplementedError


_BY_EXT: dict[str, Extractor] = {}
_BY_NAME: dict[str, Extractor] = {}


def register(extractor: Extractor) -> Extractor:
    for ext in extractor.extensions:
        _BY_EXT[ext.lower()] = extractor
    for name in extractor.filenames:
        _BY_NAME[name] = extractor
    return extractor


def get_extractor(path: str) -> Extractor | None:
    p = PurePosixPath(path)
    return _BY_NAME.get(p.name) or _BY_EXT.get(p.suffix.lower())


# Languages we recognise for stats/search filtering even without a symbol extractor.
_EXTRA_LANGS = {
    ".json": "json", ".jsonc": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".xml": "xml", ".csproj": "msbuild", ".props": "msbuild",
    ".targets": "msbuild", ".sln": "sln", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".ps1": "powershell", ".psm1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".sql": "sql", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".swift": "swift", ".vue": "vue", ".svelte": "svelte",
    ".razor": "razor", ".cshtml": "razor", ".fs": "fsharp", ".vb": "vbnet",
    ".graphql": "graphql", ".proto": "protobuf", ".txt": "text", ".rst": "rst",
    ".r": "r", ".lua": "lua", ".dart": "dart", ".tf": "terraform",
}
_EXTRA_NAMES = {"Dockerfile": "docker", "Makefile": "make", "CMakeLists.txt": "cmake"}


def detect_language(path: str) -> str:
    ex = get_extractor(path)
    if ex:
        return ex.language
    p = PurePosixPath(path)
    return _EXTRA_NAMES.get(p.name) or _EXTRA_LANGS.get(p.suffix.lower(), "other")


# ---------------------------------------------------------------- helpers for regex parsers


class LineMap:
    """Maps character offsets to 1-based line numbers."""

    def __init__(self, text: str):
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


def strip_c_like(text: str, *, verbatim_strings: bool = False, template_literals: bool = False,
                 regex_literals: bool = False) -> str:
    """Blank out comments and string contents (keeping newlines and quote chars) so brace
    matching and regexes are not confused by them. Offsets/line numbers are preserved."""
    out = list(text)
    n = len(text)
    i = 0

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif verbatim_strings and (
            (c == "@" and nxt == '"')
            or (c in "$@" and nxt in "$@" and nxt != c and text[i + 2:i + 3] == '"')
        ):
            # C# verbatim string @"..." / $@"..." where "" escapes a quote and newlines are allowed
            q = text.index('"', i)
            j = q + 1
            while j < n:
                if text[j] == '"':
                    if text[j + 1:j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            blank(q + 1, j)
            i = j + 1
        elif c in "\"'" or (template_literals and c == "`"):
            q = c
            j = i + 1
            while j < n and text[j] != q:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and q != "`":
                    break
                j += 1
            blank(i + 1, min(j, n))
            i = j + 1
        elif regex_literals and c == "/" and _regex_can_start(text, i):
            j = i + 1
            in_class = False
            while j < n and text[j] != "\n":
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            if j < n and text[j] == "/":
                blank(i + 1, j)
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def _regex_can_start(text: str, i: int) -> bool:
    k = i - 1
    while k >= 0 and text[k] in " \t":
        k -= 1
    if k < 0:
        return True
    prev = text[k]
    if prev in "(,=:[!&|?{};+-*%<>~^\n":
        return True
    m = re.search(r"(\w+)$", text[max(0, k - 10):k + 1])
    return bool(m and m.group(1) in {"return", "typeof", "case", "in", "of", "delete", "void"})


def match_braces(stripped: str, open_ch: str = "{", close_ch: str = "}") -> dict[int, int]:
    """Return {offset_of_open: offset_of_matching_close} for a comment/string-stripped text."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for i, c in enumerate(stripped):
        if c == open_ch:
            stack.append(i)
        elif c == close_ch and stack:
            pairs[stack.pop()] = i
    return pairs


def block_end(stripped: str, pairs: dict[int, int], start: int, stop_chars: str = ";") -> int | None:
    """Find the first '{' at/after ``start`` (before any stop char at depth 0) and return the
    offset of its matching '}'."""
    depth = 0
    for i in range(start, min(len(stripped), start + 2000)):
        c = stripped[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "{" and depth <= 0:
            return pairs.get(i)
        elif c in stop_chars and depth <= 0:
            return None
    return None


def first_line(s: str, limit: int = 160) -> str:
    for line in s.strip().splitlines():
        line = line.strip()
        if line and not line.startswith(("!!!", ".. ", ":::")):  # skip doc-tool directives
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def compact(s: str, limit: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def assign_parents(symbols: list[Symbol], containers: list[tuple[int, int, str]]) -> None:
    """Set ``parent`` for each symbol to the innermost container (start_line, end_line, name)
    enclosing it (a container never parents itself)."""
    for s in symbols:
        best = None
        for a, b, name in containers:
            if a <= s.line <= b and not (a == s.line and name == s.name):
                if best is None or (b - a) < (best[1] - best[0]):
                    best = (a, b, name)
        if best and s.parent is None:
            s.parent = best[2]
