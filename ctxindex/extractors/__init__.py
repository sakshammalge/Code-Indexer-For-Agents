"""Pluggable symbol extractors.

To add a language, create a module with an ``Extractor`` subclass, call ``register()`` on an
instance, and import the module below.
"""

from .base import Extractor, FileInfo, Symbol, detect_language, get_extractor, register
from . import python, javascript, csharp, css, html, markdown  # noqa: F401  (registers extractors)

__all__ = ["Extractor", "FileInfo", "Symbol", "detect_language", "get_extractor", "register"]
