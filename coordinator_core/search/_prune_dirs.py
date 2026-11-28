"""Leaf: the directory names a search prunes by default, importable without the search engine's closure."""

from __future__ import annotations

DEFAULT_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".next", "site-packages", ".gradle", "target",
})
