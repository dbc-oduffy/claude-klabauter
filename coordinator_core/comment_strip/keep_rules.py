from __future__ import annotations

import re
from typing import Iterable

_TOOLING_PATTERNS = [
    re.compile(r"^#!"),
    re.compile(r"coding[:=]\s*[-\w.]+"),
    re.compile(r"\bnoqa\b", re.I),
    re.compile(r"type:\s*ignore"),
    re.compile(r"\bpragma\b", re.I),
    re.compile(r"\bfmt:\s*(on|off)\b"),
    re.compile(r"\bisort:\s*\w+"),
    re.compile(r"\bpylint:\s*\w+"),
    re.compile(r"\bmypy:\s*\w+"),
    re.compile(r"eslint-disable"),
    re.compile(r"@ts-(ignore|expect-error|nocheck|check)"),
    re.compile(r"prettier-ignore"),
    re.compile(r"(istanbul|c8)\s+ignore", re.I),
    re.compile(r"///\s*<reference"),
    re.compile(r"#\s*(region|endregion)\b", re.I),
    re.compile(r"#pragma\b"),
    re.compile(r"SPDX-License-Identifier"),
    re.compile(r"\bcopyright\b", re.I),
    re.compile(r"\blicense\b", re.I),
    re.compile(r"UPROPERTY|UFUNCTION|UCLASS|USTRUCT|UENUM", re.I),
    re.compile(r"^//~"),
    re.compile(r"yaml-language-server"),
    re.compile(r"renovate"),
    re.compile(r"@type\b"),
    re.compile(r"@param\s*\{"),
    re.compile(r"@typedef\b"),
    re.compile(r"@returns\s*\{"),
    re.compile(r"#\s*sourceMappingURL="),
    re.compile(r"#\s*sourceURL="),
    re.compile(r"@license\b"),
    re.compile(r"@preserve\b"),
    re.compile(r"^/\*!"),
    re.compile(r"shellcheck\s+(disable|source|enable)", re.I),
    re.compile(r"^#Requires\b"),
    re.compile(r"^\.(SYNOPSIS|DESCRIPTION|PARAMETER|EXAMPLE|NOTES|LINK|INPUTS|OUTPUTS)\b", re.I | re.M),
    re.compile(r"/\*\+"),
]

_MARKER_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9_\-]{7,}")
_TAG_RE = re.compile(r"\b[\w-]+:\s*\S")
_SENTINEL_RE = re.compile(r"\b(BEGIN|END)\b.{0,40}", re.I)


def is_tooling_comment(text: str) -> bool:
    for pat in _TOOLING_PATTERNS:
        if pat.search(text):
            return True
    return False


def extract_marker_tokens(text: str) -> list[str]:
    tokens: set[str] = set()
    for m in _MARKER_TOKEN_RE.finditer(text):
        tokens.add(m.group(0))
    for m in re.finditer(r"\btripwire[:\-]\S+", text, re.I):
        tokens.add(m.group(0))
    for m in _SENTINEL_RE.finditer(text):
        tokens.add(m.group(0).strip())
    return [t for t in tokens if len(t) >= 8]
