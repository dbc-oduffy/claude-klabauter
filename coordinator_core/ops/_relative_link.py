
"""Normalizes a repo-relative link target and rewrites it relative to a given output file's own directory, for generated markdown that must not assume it renders at the repo root."""

from __future__ import annotations

import os


def normalize_repo_relative(target: str) -> str:
    parts = target.split("/")
    while parts and parts[0] in (".", ".."):
        parts.pop(0)
    return "/".join(parts)


def relative_markdown_target(target: str, out_path: str) -> str:
    normalized = normalize_repo_relative(target)
    rel = os.path.relpath(normalized, start=os.path.dirname(out_path))
    return rel.replace("\\", "/")
