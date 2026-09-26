from __future__ import annotations

from dataclasses import dataclass

from .patterns import PATTERNS

EXEMPT_PATH_SEGMENTS = (
    "state/",
    "docs/plans/",
    "tasks/",
    "archive/",
    "handoffs/",
    "docs/wiki/",
    "docs/decisions/",
    "sidecar",
    ".coordinator-local/subagent-share/",
)


@dataclass(frozen=True)
class Match:
    pattern: str
    span: tuple[int, int]
    text: str


def is_exempt_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    if normalized.endswith(".md"):
        for seg in ("docs/wiki/", "docs/decisions/", "docs/plans/", "handoffs/", "state/", "archive/", "tasks/"):
            if seg in normalized:
                return True
    for seg in EXEMPT_PATH_SEGMENTS:
        if seg in normalized:
            return True
    return False


def scan_text(text: str, path: str) -> list[Match]:
    if is_exempt_path(path):
        return []
    matches: list[Match] = []
    for name, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            matches.append(Match(pattern=name, span=m.span(), text=m.group(0)))
    return matches
