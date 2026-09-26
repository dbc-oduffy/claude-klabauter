from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Span:
    start: int
    end: int
    text: str


_HEREDOC_RE = re.compile(r"<<[-~]?\s*(['\"]?)(\w+)\1")
_PS_HERESTRING_RE = re.compile(r"@([\"'])")
_WORD_BOUNDARY_CHARS = " \t\n;|&()<>{}[]!﻿"


def _is_comment_start(s: str, i: int) -> bool:
    """`#` starts a comment only at a word boundary — preceded by whitespace, a shell/PS
    metacharacter, or start-of-text. Excludes `${#x}`/`$#` (preceded by `$`/`{`) and a mid-word
    `#` like `foo#bar` or a YAML/TOML unquoted scalar `http://x#frag`, none of which are
    comments in bash, PowerShell, YAML, or TOML."""
    return i == 0 or s[i - 1] in _WORD_BOUNDARY_CHARS


def find_comment_spans(s: str, *, shell_heredocs: bool = False,
                        powershell_block: bool = False) -> list[Span]:
    spans: list[Span] = []
    i = 0
    n = len(s)
    in_str: str | None = None
    pending_heredocs: list[str] = []
    while i < n:
        c = s[i]
        if in_str is not None:
            if in_str == "ps-here-dq" or in_str == "ps-here-sq":
                # PowerShell here-string: closes only at a line-start `"@`/`'@`.
                closer = '"@' if in_str == "ps-here-dq" else "'@"
                if s.startswith(closer, i) and (i == 0 or s[i - 1] == "\n"):
                    in_str = None
                    i += len(closer)
                    continue
                i += 1
                continue
            # Backslash escapes a quote character in shell double-quoted strings, but NOT
            # in PowerShell (backtick is PS's escape char; backslash is literal there) — using
            # the shell rule for `.ps1` swallows a real closing quote after a Windows path
            # ending in a backslash (abs-path-ok: illustrative example, not a real path),
            # corrupting everything after it.
            if c == "\\" and in_str == '"' and not powershell_block:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if powershell_block and s.startswith("<#", i):
            j = s.find("#>", i + 2)
            j = (j + 2) if j != -1 else n
            spans.append(Span(i, j, s[i:j]))
            i = j
            continue
        if powershell_block:
            m = _PS_HERESTRING_RE.match(s, i)
            # A here-string opener (`@"`/`@'`) is only valid immediately followed by a
            # newline; `@"foo"` on one line is ordinary string concatenation, not a here-string.
            if m and s[i + 2:i + 3] == "\n":
                in_str = "ps-here-dq" if m.group(1) == '"' else "ps-here-sq"
                i += 3
                continue
        if c in ("'", '"'):
            in_str = c
            i += 1
            continue
        if shell_heredocs and s.startswith("<<", i):
            m = _HEREDOC_RE.match(s, i)
            if m:
                pending_heredocs.append(m.group(2))
                i = m.end()
                continue
        if c == "\n":
            if shell_heredocs and pending_heredocs:
                delim = pending_heredocs.pop(0)
                j = s.find("\n" + delim, i)
                end_line = s.find("\n", j + 1) if j != -1 else n
                i = (end_line if end_line != -1 else n) if j != -1 else n
                continue
            i += 1
            continue
        if c == "#" and _is_comment_start(s, i):
            j = s.find("\n", i)
            j = j if j != -1 else n
            spans.append(Span(i, j, s[i:j]))
            i = j
            continue
        i += 1
    return spans
