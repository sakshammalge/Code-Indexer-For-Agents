"""Markdown extractor: heading sections (ATX and setext), skipping fenced code blocks."""

from __future__ import annotations

import re

from .base import Extractor, FileInfo, Symbol, compact, register

_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


class MarkdownExtractor(Extractor):
    language = "markdown"
    extensions = (".md", ".markdown", ".mdx")

    def extract(self, text: str, path: str) -> FileInfo:
        lines = text.splitlines()
        heads: list[tuple[int, int, str]] = []  # (line, level, title)
        in_fence = None
        for i, line in enumerate(lines):
            fm = _FENCE.match(line)
            if fm:
                if in_fence is None:
                    in_fence = fm.group(1)
                elif fm.group(1) == in_fence:
                    in_fence = None
                continue
            if in_fence:
                continue
            m = _ATX.match(line)
            if m:
                heads.append((i + 1, len(m.group(1)), _clean(m.group(2))))
            elif i > 0 and lines[i - 1].strip() and re.fullmatch(r"\s*(=+|-+)\s*", line) \
                    and not lines[i - 1].lstrip().startswith(("-", "*", "|", ">")) and not _ATX.match(lines[i - 1]):
                if len(line.strip()) >= 3 or line.strip().startswith("="):
                    heads.append((i, 1 if "=" in line else 2, _clean(lines[i - 1])))

        symbols: list[Symbol] = []
        stack: list[tuple[int, str]] = []
        for idx, (line, level, title) in enumerate(heads):
            end = len(lines)
            for nline, nlevel, _ in heads[idx + 1:]:
                if nlevel <= level:
                    end = nline - 1
                    break
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else None
            symbols.append(Symbol(name=title, kind="section", line=line, end_line=max(line, end),
                                  signature="#" * level + " " + title, parent=parent,
                                  docstring=_first_para(lines, line)))
            stack.append((level, title))
        doc = next((s.name for s in symbols if s.signature.startswith("# ")), symbols[0].name if symbols else "")
        return FileInfo(symbols=symbols, doc=doc)


def _clean(s: str) -> str:
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)  # links/images -> text
    s = re.sub(r"[`*_]", "", s)
    return compact(s.strip(), 120)


def _first_para(lines: list[str], heading_line: int) -> str:
    for line in lines[heading_line: heading_line + 8]:
        s = line.strip()
        if not s or s.startswith(("```", "~~~", "|", "<", "![")) or set(s) <= set("=-"):
            continue
        if s.startswith("#"):
            return ""
        return compact(_clean(s), 160)
    return ""


register(MarkdownExtractor())
