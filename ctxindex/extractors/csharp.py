"""C# / .NET extractor: namespaces, types, methods, constructors, properties, delegates."""

from __future__ import annotations

import re

from .base import (
    Extractor, FileInfo, LineMap, Symbol, assign_parents, block_end, compact, match_braces,
    register, strip_c_like,
)

_MODS = (r"(?:(?:public|private|protected|internal|static|virtual|override|abstract|async|sealed|"
         r"extern|new|partial|unsafe|readonly|required|file|volatile|const)\s+)*")
_ATTRS = r"(?:\[[^\]\n]*\]\s*)*"

_NAMESPACE = re.compile(r"^[ \t]*namespace\s+(?P<name>[\w.]+)\s*(?P<term>[;{])", re.M)
_TYPE = re.compile(
    r"^[ \t]*" + _ATTRS + _MODS +
    r"(?P<kw>class|interface|struct|enum|record(?:\s+struct|\s+class)?)\s+"
    r"(?P<name>[A-Za-z_]\w*)(?P<rest>[^{;]*)",
    re.M,
)
_DELEGATE = re.compile(
    r"^[ \t]*" + _MODS + r"delegate\s+[\w<>\[\],.?\s]+?\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*;",
    re.M,
)
_METHOD = re.compile(
    r"^[ \t]*" + _ATTRS + r"(?P<mods>" + _MODS + r")"
    r"(?P<ret>[A-Za-z_][\w<>\[\],.?: ]*?\s+)?(?P<name>[A-Za-z_]\w*)\s*(?P<generic><[^>()]*>)?\s*"
    r"(?P<params>\([^)]*\))\s*(?:where\s[^{;=]*)?(?::\s*(?:base|this)\s*\([^)]*\)\s*)?(?P<body>\{|=>|;)",
    re.M,
)
_PROPERTY = re.compile(
    r"^[ \t]*" + _ATTRS + _MODS +
    r"(?P<type>[A-Za-z_][\w<>\[\],.?]*(?:\s*<[^>]*>)?\??)\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"(?:\{\s*(?:get|set|init|private|protected|internal|public)\b|=>)",
    re.M,
)
_NOT_METHOD = {"if", "for", "foreach", "while", "switch", "catch", "using", "lock", "return", "nameof",
               "typeof", "sizeof", "new", "else", "throw", "await", "base", "this", "fixed", "when",
               "get", "set", "init", "add", "remove", "var", "default", "checked", "unchecked"}
_KIND = {"class": "class", "interface": "interface", "struct": "struct", "enum": "enum"}


class CSharpExtractor(Extractor):
    language = "csharp"
    extensions = (".cs", ".csx")

    def extract(self, text: str, path: str) -> FileInfo:
        stripped = strip_c_like(text, verbatim_strings=True)
        lm = LineMap(text)
        pairs = match_braces(stripped)
        symbols: list[Symbol] = []
        containers: list[tuple[int, int, str]] = []  # namespaces and types
        type_names: set[str] = set()
        last_line = lm.line(len(text))

        for m in _NAMESPACE.finditer(stripped):
            line = lm.line(m.start("name"))
            if m.group("term") == ";":  # file-scoped namespace
                end = last_line
            else:
                close = pairs.get(m.end("term") - 1)
                end = lm.line(close) if close is not None else last_line
            symbols.append(Symbol(name=m.group("name"), kind="namespace", line=line, end_line=end,
                                  signature=f"namespace {m.group('name')}"))
            containers.append((line, end, m.group("name")))

        for m in _TYPE.finditer(stripped):
            name = m.group("name")
            kw = " ".join(m.group("kw").split())
            line = lm.line(m.start("name"))
            close = block_end(stripped, pairs, m.start("name"))
            end = lm.line(close) if close is not None else line
            kind = _KIND.get(kw, "record")
            head = text[m.start():m.end()]
            symbols.append(Symbol(name=name, kind=kind, line=line, end_line=end,
                                  signature=compact(_strip_attrs(head), 180),
                                  docstring=_xml_doc_before(text, m.start())))
            containers.append((line, end, name))
            type_names.add(name)

        for m in _DELEGATE.finditer(stripped):
            line = lm.line(m.start("name"))
            symbols.append(Symbol(name=m.group("name"), kind="delegate", line=line, end_line=line,
                                  signature=compact(text[m.start():m.end()].strip().rstrip(";"), 180)))

        type_spans = [c for c in containers if c[2] in type_names]
        seen = {(s.name, s.line) for s in symbols}
        for m in _METHOD.finditer(stripped):
            name = m.group("name")
            ret = (m.group("ret") or "").strip()
            if name in _NOT_METHOD or ret in _NOT_METHOD or ret.split(" ")[-1:] == ["new"]:
                continue
            line = lm.line(m.start("name"))
            if (name, line) in seen:
                continue
            owner = _innermost(type_spans, line)
            if owner is None or owner[0] == line:
                continue
            if not ret and name != owner[2]:
                continue  # a bare call like `Foo(...)` — not a declaration
            if ret and ("=" in ret or ret.startswith(("return ", "await "))):
                continue
            body = m.group("body")
            end_line = line
            if body == "{":
                close = pairs.get(m.end("body") - 1)
                end_line = lm.line(close) if close is not None else line
            elif body == "=>":
                semi = stripped.find(";", m.end())
                end_line = lm.line(semi) if semi >= 0 else line
            kind = "constructor" if name == owner[2] and not ret else "method"
            seen.add((name, line))
            symbols.append(Symbol(
                name=name, kind=kind, line=line, end_line=end_line,
                signature=compact(_strip_attrs(text[m.start():m.start("body")]), 200),
                docstring=_xml_doc_before(text, m.start()),
            ))

        for m in _PROPERTY.finditer(stripped):
            name, typ = m.group("name"), m.group("type")
            if typ in _NOT_METHOD or typ in ("class", "struct", "interface", "enum", "record", "namespace"):
                continue
            line = lm.line(m.start("name"))
            if (name, line) in seen or _innermost(type_spans, line) is None:
                continue
            seen.add((name, line))
            symbols.append(Symbol(
                name=name, kind="property", line=line, end_line=line,
                signature=compact(re.sub(r"\s*(?:\{\s*\w*|=>)$", "", _strip_attrs(text[m.start():m.end()])), 160),
                docstring=_xml_doc_before(text, m.start()),
            ))

        assign_parents(symbols, containers)
        symbols.sort(key=lambda s: s.line)
        return FileInfo(symbols=symbols)


def _innermost(spans, line):
    best = None
    for a, b, n in spans:
        if a <= line <= b and (best is None or b - a < best[1] - best[0]):
            best = (a, b, n)
    return best


def _strip_attrs(s: str) -> str:
    return re.sub(r"^\s*(?:\[[^\]\n]*\]\s*)*", "", s).strip()


_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)


def _xml_doc_before(text: str, offset: int) -> str:
    lines = text[max(0, offset - 2000):offset].splitlines()
    doc: list[str] = []
    for line in reversed(lines):
        s = line.strip()
        if s.startswith("///"):
            doc.append(s[3:].strip())
        elif s.startswith("[") or not s:
            if doc or s.startswith("["):
                continue
            break
        else:
            break
    if not doc:
        return ""
    body = " ".join(reversed(doc))
    m = _SUMMARY.search(body)
    body = m.group(1) if m else body
    return compact(re.sub(r"<[^>]+>", "", body), 160)


register(CSharpExtractor())
