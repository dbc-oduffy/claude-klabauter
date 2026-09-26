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


def _is_cpp_raw_string_start(s: str, i: int) -> tuple[int, str] | None:
    """`R"delim(...)delim"` — the delimiter is any of ~16 chars, none of which are
    parens/backslash/whitespace per the standard; unlike Rust's `r#"..."#`, the delimiter
    is arbitrary text, not a hash run, so it must be read up to the opening `(`."""
    if s[i] != "R" or i + 1 >= len(s) or s[i + 1] != '"':
        return None
    j = i + 2
    start_delim = j
    while j < len(s) and s[j] not in "()\\ \t\n\"":
        j += 1
    if j >= len(s) or s[j] != "(":
        return None
    return j + 1, s[start_delim:j]


def _is_digit_separator_quote(s: str, i: int) -> bool:
    """C++14 `1'000'000` — a `'` between two alnum/hex-digit characters is a digit
    separator, never a char-literal delimiter. A real char literal is never preceded
    immediately by an alnum char (it always follows an operator, punctuation, or
    whitespace), so this test can't misfire on `x = 'a'` or `arr[i] = '\\n'`."""
    prev_ok = i > 0 and (s[i - 1].isalnum())
    next_ok = i + 1 < len(s) and (s[i + 1].isalnum())
    return prev_ok and next_ok


def find_comment_spans(s: str, *, line_comment: str = "//", block_comment: bool = True,
                        rust_raw_strings: bool = False, sql_line_comment: bool = False,
                        cpp_raw_strings: bool = False) -> list[Span]:
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
        if cpp_raw_strings:
            raw = _is_cpp_raw_string_start(s, i)
            if raw is not None:
                body_start, delim = raw
                closer = ")" + delim + '"'
                idx = s.find(closer, body_start)
                i = (idx + len(closer)) if idx != -1 else n
                continue
        if c == "'" and _is_digit_separator_quote(s, i):
            i += 1
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
