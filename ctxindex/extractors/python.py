"""Python extractor using the standard-library ``ast`` module (regex fallback on syntax errors)."""

from __future__ import annotations

import ast
import re

from .base import Extractor, FileInfo, Symbol, compact, first_line, register


def _args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        sig = f"({ast.unparse(node.args)})"
        if node.returns is not None:
            sig += f" -> {ast.unparse(node.returns)}"
        return sig
    except Exception:  # pragma: no cover - ast.unparse is robust, but be safe
        return "(...)"


def _decorators(node) -> str:
    out = []
    for d in getattr(node, "decorator_list", []):
        try:
            out.append("@" + ast.unparse(d))
        except Exception:  # pragma: no cover
            pass
    return " ".join(out)


class PythonExtractor(Extractor):
    language = "python"
    extensions = (".py", ".pyi", ".pyw")

    def extract(self, text: str, path: str) -> FileInfo:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return self._fallback(text)
        info = FileInfo(doc=first_line(ast.get_docstring(tree) or ""))
        self._walk(tree.body, None, info.symbols, in_class=False)
        return info

    def _walk(self, body, parent: str | None, out: list[Symbol], in_class: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
                deco = _decorators(node)
                kind = "method" if in_class else "function"
                if in_class and "property" in deco:
                    kind = "property"
                out.append(Symbol(
                    name=node.name, kind=kind, line=node.lineno, end_line=node.end_lineno,
                    signature=compact(f"{deco + ' ' if deco else ''}{prefix}{node.name}{_args(node)}"),
                    parent=parent, docstring=first_line(ast.get_docstring(node) or ""),
                ))
                # nested functions are rarely useful as navigation targets; skip them
            elif isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases + node.keywords) if (
                    node.bases or node.keywords) else ""
                deco = _decorators(node)
                out.append(Symbol(
                    name=node.name, kind="class", line=node.lineno, end_line=node.end_lineno,
                    signature=compact(f"{deco + ' ' if deco else ''}class {node.name}"
                                      f"{'(' + bases + ')' if bases else ''}"),
                    parent=parent, docstring=first_line(ast.get_docstring(node) or ""),
                ))
                qual = f"{parent}.{node.name}" if parent else node.name
                self._walk(node.body, qual, out, in_class=True)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and parent is None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and (t.id.isupper() or t.id == "__all__"):
                        try:
                            val = ast.unparse(node.value) if node.value is not None else ""
                        except Exception:  # pragma: no cover
                            val = ""
                        out.append(Symbol(
                            name=t.id, kind="constant", line=node.lineno, end_line=node.end_lineno,
                            signature=compact(f"{t.id} = {val}", 120),
                        ))
            elif isinstance(node, (ast.If, ast.Try)) and parent is None:
                # top-level conditional definitions, e.g. `if TYPE_CHECKING:` / try-import
                self._walk(node.body, parent, out, in_class)

    _DEF_RE = re.compile(r"^(\s*)(async\s+def|def|class)\s+([A-Za-z_]\w*)\s*(\([^)\n]*\)?)?", re.M)

    def _fallback(self, text: str) -> FileInfo:
        """Regex outline for files that don't parse (e.g. mid-edit); parents from indentation."""
        info = FileInfo()
        classes: list[tuple[int, str]] = []  # (indent width, qualified name)
        for m in self._DEF_RE.finditer(text):
            indent, kw, name, args = len(m.group(1).expandtabs()), m.group(2), m.group(3), m.group(4) or ""
            line = text.count("\n", 0, m.start()) + 1
            while classes and classes[-1][0] >= indent:
                classes.pop()
            parent = classes[-1][1] if classes else None
            kind = "class" if kw == "class" else ("method" if parent else "function")
            info.symbols.append(Symbol(name=name, kind=kind, line=line, end_line=line, parent=parent,
                                       signature=compact(f"{kw} {name}{args}")))
            if kw == "class":
                classes.append((indent, f"{parent}.{name}" if parent else name))
        return info


register(PythonExtractor())
