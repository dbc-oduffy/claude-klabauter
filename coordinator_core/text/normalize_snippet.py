from __future__ import annotations

_TRAILING_WHITESPACE = " \t\n\v\f\r"


def normalize_snippet(text: str) -> str:
    lines = [line.rstrip(_TRAILING_WHITESPACE) for line in text.split("\n")]

    first = next((i for i, line in enumerate(lines) if line != ""), None)
    if first is None:
        return ""
    last = max(i for i, line in enumerate(lines) if line != "")
    return "\n".join(lines[first : last + 1])
