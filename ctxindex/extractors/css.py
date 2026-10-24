"""CSS / SCSS / Less extractor: selectors, at-rules, custom properties, SCSS variables & mixins."""

from __future__ import annotations

import re

from .base import Extractor, FileInfo, LineMap, Symbol, compact, register, strip_c_like

_CUSTOM_PROP = re.compile(r"(?<![\w-])(--[A-Za-z0-9_-]+)\s*:\s*([^;}]*)")
_SCSS_VAR = re.compile(r"^(\$[A-Za-z0-9_-]+)\s*:\s*([^;]*)", re.M)  # top-level only
_AT_NAMED = re.compile(r"@(mixin|function|keyframes|-webkit-keyframes|font-face|layer|container)\b\s*([^\s({;]*)")


class CssExtractor(Extractor):
    language = "css"
    extensions = (".css",)

    def extract(self, text: str, path: str) -> FileInfo:
        # SCSS/Less have // comments; protect unquoted url(http://...) from being cut by them.
        safe = re.sub(r"url\(\s*[^'\")]*\)", lambda m: "url(" + " " * (len(m.group(0)) - 5) + ")", text)
        stripped = strip_c_like(safe)
        # SCSS interpolation #{...} is not a block; blank it (same length keeps offsets).
        stripped = re.sub(r"#\{[^{}]*\}", lambda m: "_" * len(m.group(0)), stripped)
        lm = LineMap(text)
        symbols: list[Symbol] = []
        stack: list[tuple[str, int, int]] = []  # (name, symbol index or -1, line)
        seg_start = 0
        for i, c in enumerate(stripped):
            if c == "{":
                head = stripped[seg_start:i].strip()
                head_off = seg_start + (len(stripped[seg_start:i]) - len(stripped[seg_start:i].lstrip()))
                parent = next((n for n, idx, _ in reversed(stack) if idx >= 0), None)
                if head.startswith("#{") or not head:
                    stack.append(("", -1, 0))
                elif head.startswith("@"):
                    m = _AT_NAMED.match(head)
                    if m and m.group(1) in ("mixin", "function", "keyframes", "-webkit-keyframes"):
                        kind = {"mixin": "mixin", "function": "function"}.get(m.group(1), "keyframes")
                        name = m.group(2) or m.group(1)
                    else:
                        kind, name = "at-rule", compact(head, 80)
                    symbols.append(Symbol(name=name, kind=kind, line=lm.line(head_off),
                                          signature=compact(head, 160), parent=parent))
                    stack.append((name, len(symbols) - 1, 0))
                elif _looks_like_property(head):
                    stack.append(("", -1, 0))  # nested property block (e.g. font: { ... })
                else:
                    raw = text[head_off:i].strip()  # original text keeps #{...} interpolation
                    name = compact(raw, 120)
                    symbols.append(Symbol(name=name, kind="selector", line=lm.line(head_off),
                                          signature=compact(raw, 160), parent=parent))
                    stack.append((name, len(symbols) - 1, 0))
                seg_start = i + 1
            elif c == "}":
                if stack:
                    _, idx, _ = stack.pop()
                    if idx >= 0:
                        symbols[idx].end_line = lm.line(i)
                seg_start = i + 1
            elif c == ";":
                seg_start = i + 1

        for m in _CUSTOM_PROP.finditer(stripped):
            name = text[m.start(1):m.end(1)]  # original text keeps #{$prefix} interpolation
            value = text[m.start(2):m.end(2)]
            symbols.append(Symbol(name=name, kind="variable", line=lm.line(m.start()),
                                  signature=compact(f"{name}: {value}", 120)))
        for m in _SCSS_VAR.finditer(stripped):
            value = text[m.start(2):m.end(2)]
            symbols.append(Symbol(name=m.group(1), kind="variable", line=lm.line(m.start(1)),
                                  signature=compact(f"{m.group(1)}: {value}", 120)))
        for s in symbols:
            if s.end_line is None:
                s.end_line = s.line
        symbols.sort(key=lambda s: s.line)
        return FileInfo(symbols=symbols)


def _looks_like_property(head: str) -> bool:
    return bool(re.fullmatch(r"[a-z-]+\s*:", head))


class ScssExtractor(CssExtractor):
    language = "scss"
    extensions = (".scss", ".sass", ".less")


register(CssExtractor())
register(ScssExtractor())
