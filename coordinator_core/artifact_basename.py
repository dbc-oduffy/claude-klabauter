
from __future__ import annotations


def md_fallback_candidates(basename: str) -> list[str]:
    candidates = [basename]
    if not basename.endswith(".md"):
        candidates.append(basename + ".md")
    return candidates
