from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Span:
    start: int
    end: int
    text: str


def _is_rust_raw_string_start(s: str, i: int) -> int | None:
    if s[i] != "r":
        return None
    j = i + 1
    hashes = 0
    while j < len(s) and s[j] == "#":
        hashes += 1
        j += 1
    if j < len(s) and s[j] == '"':
        return hashes
    return None


def find_comment_spans(s: str, *, line_comment: str = "//", block_comment: bool = True,
                        rust_raw_strings: bool = False, sql_line_comment: bool = False) -> list[Span]:
    spans: list[Span] = []
    i = 0
    n = len(s)
    in_str = None
    while i < n:
        c = s[i]
        if in_str is not None:
            if c == "\\" and in_str != "`raw`":
                i += 2
                continue
            if in_str == "`raw`":
                pass
            if c == in_str:
                in_str = None
            i += 1
            continue
        if rust_raw_strings:
            hashes = _is_rust_raw_string_start(s, i)
            if hashes is not None:
                i += 1 + hashes + 1
                closer = '"' + "#" * hashes
                idx = s.find(closer, i)
                i = (idx + len(closer)) if idx != -1 else n
                continue
        if c in ("'", '"'):
            in_str = c
            i += 1
            continue
        if line_comment and s.startswith(line_comment, i):
            j = s.find("\n", i)
            j = j if j != -1 else n
            spans.append(Span(i, j, s[i:j]))
            i = j
            continue
        if sql_line_comment and s.startswith("--", i):
            j = s.find("\n", i)
            j = j if j != -1 else n
            spans.append(Span(i, j, s[i:j]))
            i = j
            continue
        if block_comment and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = (j + 2) if j != -1 else n
            spans.append(Span(i, j, s[i:j]))
            i = j
            continue
        i += 1
    return spans
