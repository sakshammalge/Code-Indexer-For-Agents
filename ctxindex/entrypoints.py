"""Heuristics for spotting entry points and key build/config files."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]

_CONFIG_FILES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "Makefile", "tsconfig.json",
    "global.json", "Directory.Build.props", "appsettings.json", "angular.json", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "CMakeLists.txt",
}
_CONFIG_PREFIXES = ("vite.config.", "webpack.config.", "next.config.", "rollup.config.",
                    "jest.config.", "vitest.config.", "babel.config.", "tailwind.config.")
_MAIN_NAMES = {
    "main.py": "main module", "app.py": "app module", "manage.py": "Django manage.py",
    "wsgi.py": "WSGI app", "asgi.py": "ASGI app", "__main__.py": "`python -m` entry",
    "Program.cs": ".NET program entry", "Startup.cs": "ASP.NET startup",
    "index.html": "HTML page entry", "server.js": "server entry", "server.ts": "server entry",
}
_JS_MAIN = re.compile(r"^(?:src/)?(?:index|main|app)\.(?:js|jsx|ts|tsx|mjs)$")
_CS_MAIN = re.compile(r"\bstatic\s+(?:async\s+)?(?:void|int|Task(?:<int>)?)\s+Main\s*\(")
_PY_MAIN = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)


def detect(path: str, text: str) -> str | None:
    """Return a short reason string if ``path`` looks like an entry point / key config file."""
    p = PurePosixPath(path)
    name = p.name
    depth = len(p.parts) - 1
    if name == "package.json":
        return _package_json(text)
    if name == "pyproject.toml":
        return _pyproject(text)
    if name.endswith(".csproj"):
        kind = "executable" if re.search(r"<OutputType>\s*(?:Win)?Exe\s*<", text, re.I) or \
            'Sdk="Microsoft.NET.Sdk.Web"' in text else "library"
        tf = re.search(r"<TargetFrameworks?>([^<]+)<", text)
        return f".NET project ({kind}{', ' + tf.group(1) if tf else ''})"
    if name.endswith(".sln"):
        return ".NET solution"
    if name in _MAIN_NAMES and (depth <= 2 or name in ("__main__.py", "Program.cs", "manage.py")):
        return _MAIN_NAMES[name]
    if p.suffix == ".py" and _PY_MAIN.search(text):
        return "script (`if __name__ == \"__main__\"`)"
    if p.suffix == ".cs" and _CS_MAIN.search(text):
        return ".NET Main()"
    if _JS_MAIN.match("/".join(p.parts[-2:]) if depth else name) and depth <= 2:
        return "JS/TS entry module"
    if name in _CONFIG_FILES or name.startswith(_CONFIG_PREFIXES):
        return "build/config"
    return None


def _package_json(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return "npm package"
    bits = []
    if isinstance(data.get("main"), str):
        bits.append(f"main={data['main']}")
    if isinstance(data.get("bin"), (str, dict)):
        b = data["bin"]
        bits.append("bin=" + (b if isinstance(b, str) else ",".join(b)))
    scripts = data.get("scripts")
    if isinstance(scripts, dict) and scripts:
        bits.append("scripts: " + ", ".join(list(scripts)[:6]))
    return "npm package" + (" (" + "; ".join(bits) + ")" if bits else "")


def _pyproject(text: str) -> str:
    if tomllib is None:
        return "Python project"
    try:
        data = tomllib.loads(text)
    except Exception:
        return "Python project"
    scripts = data.get("project", {}).get("scripts") or \
        data.get("tool", {}).get("poetry", {}).get("scripts") or {}
    if scripts:
        return "Python project (console scripts: " + ", ".join(f"{k} → {v}" for k, v in list(scripts.items())[:4]) + ")"
    return "Python project"
