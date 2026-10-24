"""JavaScript / TypeScript (incl. JSX/TSX) extractor based on regexes plus brace matching."""

from __future__ import annotations

import re

from .base import (
    Extractor, FileInfo, LineMap, Symbol, assign_parents, block_end, compact, match_braces,
    register, strip_c_like,
)

_EXPORT = r"(?P<export>export\s+(?:default\s+)?)?"
_DECLS = [
    ("function", re.compile(
        r"^[ \t]*" + _EXPORT + r"(?:declare\s+)?(?P<async>async\s+)?function\s*\*?\s*"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*(?P<generic><[^>{}]*>)?\s*(?P<params>\([^)]*\))", re.M)),
    ("class", re.compile(
        r"^[ \t]*" + _EXPORT + r"(?:declare\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"
        r"(?P<rest>[^{]*)", re.M)),
    ("interface", re.compile(
        r"^[ \t]*" + _EXPORT + r"(?:declare\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)(?P<rest>[^{]*)",
        re.M)),
    ("type", re.compile(
        r"^[ \t]*" + _EXPORT + r"(?:declare\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)\s*[<=]",
        re.M)),
    ("enum", re.compile(
        r"^[ \t]*" + _EXPORT + r"(?:declare\s+)?(?:const\s+)?enum\s+(?P<name>[A-Za-z_$][\w$]*)", re.M)),
    ("namespace", re.compile(
        r"^[ \t]*" + _EXPORT + r"(?:declare\s+)?(?:namespace|module)\s+(?P<name>[A-Za-z_$][\w$.]*)\s*\{",
        re.M)),
    # const foo = (a) => ..., const foo = async function (...), const Foo = React.memo(...)
    ("function", re.compile(
        r"^[ \t]*" + _EXPORT + r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+?)?=\s*"
        r"(?P<async>async\s+)?(?:(?P<params>\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+?)?=>|function\b)",
        re.M)),
    # top-level (column 0) constants: export const API_URL = ..., const config = {...}
    ("variable", re.compile(
        r"^" + _EXPORT + r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=(?!\s*(?:async\b|function\b|\())",
        re.M)),
]

_METHOD = re.compile(
    r"^[ \t]+(?:(?:public|private|protected|static|readonly|abstract|override|async|get|set|declare)\s+)*"
    r"(?P<star>\*\s*)?(?P<name>#?[A-Za-z_$][\w$]*)\s*(?P<opt>\?)?\s*(?P<generic><[^>{}()]*>)?\s*"
    r"(?P<params>\([^)]*\))\s*(?::\s*[^{;]+)?\{",
    re.M,
)
_CLASS_FIELD_ARROW = re.compile(
    r"^[ \t]+(?:(?:public|private|protected|static|readonly)\s+)*(?P<name>#?[A-Za-z_$][\w$]*)\s*(?::[^=]+)?="
    r"\s*(?:async\s+)?(?P<params>\([^)]*\))\s*(?::[^=]+)?=>",
    re.M,
)
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "with", "do", "else",
             "super", "typeof", "new", "await", "yield"}
# tool pragmas and license boilerplate are not descriptions
_DIRECTIVE = re.compile(r"(?:@|#!|eslint|prettier|istanbul|jshint|global\s|c8\s|copyright|\(c\)|spdx|licen[cs]ed)", re.I)
_LEADING_COMMENT = re.compile(r"\A\s*(?:/\*\*?(?P<block>.*?)\*/|(?P<lines>(?:\s*//[^\n]*\n)+))", re.S)


class JavaScriptExtractor(Extractor):
    language = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")

    def extract(self, text: str, path: str) -> FileInfo:
        stripped = strip_c_like(text, template_literals=True, regex_literals=True)
        lm = LineMap(text)
        pairs = match_braces(stripped)
        is_jsx = path.endswith((".jsx", ".tsx")) or "React" in text
        symbols: list[Symbol] = []
        containers: list[tuple[int, int, str]] = []
        func_spans: list[tuple[int, int, str]] = []
        seen: set[tuple[str, int]] = set()

        for kind, rx in _DECLS:
            for m in rx.finditer(stripped):
                name = m.group("name")
                line = lm.line(m.start("name"))
                if (name, line) in seen:
                    continue
                seen.add((name, line))
                end_off = None
                if kind in ("function", "class", "interface", "enum", "namespace"):
                    end_off = block_end(stripped, pairs, m.end() - 1 if kind == "namespace" else m.start("name"))
                end_line = lm.line(end_off) if end_off is not None else line
                k = kind
                if kind == "function" and is_jsx and name[:1].isupper():
                    k = "component"
                sig = compact(text[m.start():m.end()].strip().rstrip("={").strip(), 160)
                symbols.append(Symbol(name=name, kind=k, line=line, end_line=end_line, signature=sig,
                                      docstring=_jsdoc_before(text, m.start())))
                if kind in ("class", "interface", "namespace") and end_off is not None:
                    containers.append((line, end_line, name))
                elif kind == "function" and end_line > line:
                    func_spans.append((line, end_line, name))

        # methods: only inside class bodies
        class_spans = list(containers)
        containers += func_spans  # nested helpers get their enclosing function as parent
        for rx in (_METHOD, _CLASS_FIELD_ARROW):
            for m in rx.finditer(stripped):
                name = m.group("name")
                if name in _KEYWORDS:
                    continue
                line = lm.line(m.start("name"))
                owner = _innermost(class_spans, line)
                if owner is None or owner[0] == line or (name, line) in seen:
                    continue
                seen.add((name, line))
                end_off = block_end(stripped, pairs, m.start("params")) if rx is _METHOD else None
                kind = "constructor" if name == "constructor" else "method"
                symbols.append(Symbol(
                    name=name, kind=kind, line=line,
                    end_line=lm.line(end_off) if end_off is not None else line,
                    signature=compact(text[m.start():m.end()].strip().rstrip("{").strip(), 160),
                    parent=owner[2], docstring=_jsdoc_before(text, m.start()),
                ))

        assign_parents(symbols, containers)
        symbols.sort(key=lambda s: s.line)
        doc = ""
        mc = _LEADING_COMMENT.match(text)
        if mc:
            raw = mc.group("block") or mc.group("lines") or ""
            raw = re.sub(r"^\s*(?:\*|//)\s?", "", raw, flags=re.M)
            doc = _first_meaningful(raw)
        return FileInfo(symbols=symbols, doc=doc)


class TypeScriptExtractor(JavaScriptExtractor):
    language = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts")


def _innermost(spans, line):
    best = None
    for a, b, n in spans:
        if a <= line <= b and (best is None or b - a < best[1] - best[0]):
            best = (a, b, n)
    return best


def _jsdoc_before(text: str, offset: int) -> str:
    before = text[max(0, offset - 1500):offset].rstrip()
    if before.endswith("*/"):
        start = before.rfind("/**")
        if start >= 0 and before[:start].strip():  # the file's leading comment is not a jsdoc
            body = before[start + 3:-2]
            body = re.sub(r"^\s*\*\s?", "", body, flags=re.M)
            return _first_meaningful(body)
    return ""


def _first_meaningful(raw: str) -> str:
    for line in raw.splitlines():
        line = line.strip()
        if line and re.search(r"[a-z]", line) and not _DIRECTIVE.match(line):
            return compact(line, 160)
    return ""


register(JavaScriptExtractor())
register(TypeScriptExtractor())
