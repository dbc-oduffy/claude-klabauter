
from __future__ import annotations

from typing import Optional


def extract_frontmatter_scalar(text: str, field: str) -> Optional[str]:
    prefix = f"{field}:"
    fence_count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            fence_count += 1
            if fence_count >= 2:
                break
        elif fence_count == 1 and line.startswith(prefix):
            rest = line[len(prefix):].strip()
            tokens = rest.split()
            if not tokens:
                return ""
            return tokens[0].strip("\"'")
    return None
