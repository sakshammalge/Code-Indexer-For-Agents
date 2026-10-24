"""Walk a project tree, honouring .gitignore files and skipping vendored/build/binary files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pathspec

SKIP_DIRS = {
    "node_modules", "bin", "obj", ".git", "dist", "build", ".venv", "venv", "__pycache__",
    ".ctxindex", ".hg", ".svn", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".gradle", ".idea", ".vs",
    "coverage", "htmlcov", ".eggs", "bower_components", "jspm_packages", ".terraform",
}
SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "uv.lock", "Cargo.lock", "composer.lock", "packages.lock.json", ".DS_Store",
}
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".icns", ".webp", ".avif", ".tif", ".tiff",
    ".psd", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".war",
    ".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".o", ".obj", ".pdb", ".class", ".pyc", ".pyo",
    ".whl", ".egg", ".nupkg", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav",
    ".ogg", ".flac", ".mov", ".avi", ".mkv", ".webm", ".sqlite", ".db", ".db-wal", ".db-shm",
    ".bin", ".dat", ".pkl", ".npy", ".npz", ".parquet", ".onnx", ".pt", ".pth", ".h5", ".snk",
    ".pfx", ".p12", ".keystore", ".map", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
}
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".chunk.js")
DEFAULT_MAX_BYTES = 1_000_000
IGNORE_FILES = (".gitignore", ".ctxindexignore")


@dataclass
class ScannedFile:
    path: str  # project-relative, forward slashes
    abspath: str
    size: int
    mtime_ns: int


def _load_spec(dirpath: str) -> pathspec.PathSpec | None:
    lines: list[str] = []
    for name in IGNORE_FILES:
        p = os.path.join(dirpath, name)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    lines.extend(fh.read().splitlines())
            except OSError:
                pass
    if not lines:
        return None
    if hasattr(pathspec, "GitIgnoreSpec"):
        return pathspec.GitIgnoreSpec.from_lines(lines)
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)  # pathspec < 0.10


def scan(root: str | Path, max_bytes: int = DEFAULT_MAX_BYTES):
    """Yield ScannedFile for every indexable file under ``root``."""
    root = os.path.abspath(str(root))
    specs: list[tuple[str, pathspec.PathSpec]] = []  # (dir rel prefix, spec), nested .gitignores

    def ignored(rel: str, is_dir: bool) -> bool:
        for base, spec in specs:
            if base and not (rel == base or rel.startswith(base + "/")):
                continue
            sub = rel[len(base) + 1:] if base else rel
            if spec.match_file(sub + "/" if is_dir else sub):
                return True
        return False

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        # drop specs belonging to directories we've left
        specs[:] = [(b, s) for b, s in specs if not b or rel_dir == b or rel_dir.startswith(b + "/")]
        spec = _load_spec(dirpath)
        if spec is not None:
            specs.append((rel_dir, spec))

        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            full = os.path.join(dirpath, d)
            if d in SKIP_DIRS or d.endswith(".egg-info") or os.path.islink(full):
                continue
            if os.path.isfile(os.path.join(full, "pyvenv.cfg")):  # any virtualenv
                continue
            if ignored(rel, True):
                continue
            keep.append(d)
        dirnames[:] = keep

        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if f in SKIP_FILES or f.lower().endswith(MINIFIED_SUFFIXES):
                continue
            if os.path.splitext(f)[1].lower() in BINARY_EXTS:
                continue
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or st.st_size > max_bytes or ignored(rel, False):
                continue
            yield ScannedFile(path=rel, abspath=full, size=st.st_size, mtime_ns=st.st_mtime_ns)


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]
