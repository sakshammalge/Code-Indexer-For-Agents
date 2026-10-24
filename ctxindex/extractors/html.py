"""HTML extractor: <title>, element ids, headings, script/stylesheet refs, inline <script>/<style>."""

from __future__ import annotations

from html.parser import HTMLParser

from .base import Extractor, FileInfo, Symbol, compact, get_extractor, register

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.symbols: list[Symbol] = []
        self.title = ""
        self._capture: tuple[str, int] | None = None  # (tag, line)
        self._buf: list[str] = []
        self._inline: tuple[str, int, int] | None = None  # (tag, line, col)
        self._inline_buf: list[str] = []
        self.inline_blocks: list[tuple[str, int, int, str]] = []  # (lang, line, col, code)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if a.get("id"):
            self.symbols.append(Symbol(name="#" + a["id"], kind="id", line=line, end_line=line,
                                       signature=compact(f"<{tag} id=\"{a['id']}\">", 120)))
        if tag == "script":
            if a.get("src"):
                self.symbols.append(Symbol(name=a["src"], kind="script", line=line, end_line=line,
                                           signature=compact(f"<script src=\"{a['src']}\">", 160)))
            elif a.get("type", "text/javascript") in ("", "module", "text/javascript", "application/javascript"):
                self._inline, self._inline_buf = ("script", line, self.getpos()[1]), []
        elif tag == "style":
            self._inline, self._inline_buf = ("style", line, self.getpos()[1]), []
        elif tag == "link" and a.get("href"):
            rel = a.get("rel", "").lower()
            kind = "stylesheet" if "stylesheet" in rel else "link"
            if kind == "stylesheet" or rel in ("icon", "manifest", "preload", "modulepreload", "import"):
                self.symbols.append(Symbol(name=a["href"], kind=kind, line=line, end_line=line,
                                           signature=compact(f"<link rel=\"{rel}\" href=\"{a['href']}\">", 160)))
        elif tag in _HEADINGS or tag == "title":
            self._capture, self._buf = (tag, line), []

    def handle_endtag(self, tag):
        if self._capture and tag == self._capture[0]:
            text = compact(" ".join(self._buf), 120)
            ctag, line = self._capture
            self._capture = None
            if ctag == "title":
                self.title = self.title or text
                if text:
                    self.symbols.append(Symbol(name=text, kind="title", line=line, end_line=self.getpos()[0]))
            elif text:
                self.symbols.append(Symbol(name=text, kind="heading", line=line,
                                           end_line=self.getpos()[0], signature=f"<{ctag}> {text}"))
        if self._inline and tag == self._inline[0]:
            kind, line, _ = self._inline
            self.inline_blocks.append(("js" if kind == "script" else "css", line, 0, "".join(self._inline_buf)))
            self._inline = None

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)
        if self._inline:
            self._inline_buf.append(data)


class HtmlExtractor(Extractor):
    language = "html"
    extensions = (".html", ".htm", ".xhtml")

    def extract(self, text: str, path: str) -> FileInfo:
        p = _Parser()
        try:
            p.feed(text)
            p.close()
        except Exception:  # malformed HTML: keep what we have
            pass
        symbols = list(p.symbols)
        # Run the JS / CSS extractors on inline blocks, shifting line numbers.
        for lang, line, _, code in p.inline_blocks:
            ex = get_extractor("inline.js" if lang == "js" else "inline.css")
            if not ex or not code.strip():
                continue
            try:
                sub = ex.extract(code, path)
            except Exception:
                continue
            offset = line - 1  # code starts on the line of the opening tag
            for s in sub.symbols:
                if lang == "css" and s.kind == "selector" and len(sub.symbols) > 40:
                    continue
                s.line += offset
                s.end_line = (s.end_line or s.line - offset) + offset
                symbols.append(s)
        symbols.sort(key=lambda s: s.line)
        return FileInfo(symbols=symbols, doc=p.title)


register(HtmlExtractor())
