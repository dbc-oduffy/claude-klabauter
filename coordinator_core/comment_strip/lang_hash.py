from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Span:
    start: int
    end: int
    text: str


_HEREDOC_RE = re.compile(r"<<[-~]?\s*(['\"]?)(\w+)\1")


def find_comment_spans(s: str, *, shell_heredocs: bool = False,
                        powershell_block: bool = False) -> list[Span]:
    spans: list[Span] = []
    i = 0
    n = len(s)
    in_str: str | None = None
    pending_heredocs: list[str] = []
    line_start = 0
    while i < n:
        c = s[i]
        if in_str is not None:
            if c == "\\" and in_str == '"':
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
            line_start = i
            continue
        if c == "#":
            j = s.find("\n", i)
            j = j if j != -1 else n
            spans.append(Span(i, j, s[i:j]))
            i = j
            continue
        i += 1
    return spans
