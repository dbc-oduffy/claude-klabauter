"""Hoist repeated static prompt text in a composed Workflow script into top-level consts.

A composed ask script inlines the same sentences in many `agent(...)` prompts; the Workflow runner
caps the script at a fixed byte size. `hoist_repeated_fragments` rewrites each single-quoted string
literal so a sentence-sized chunk that repeats across literals becomes a `_PFn` const reference.
The rewrite is concatenation-only, so every run-time string is unchanged.

Trap: template literals, comments and regex literals are skipped, never hoisted into; the scanner
decides regex-vs-division from the previous significant character.
"""

from __future__ import annotations

import re
from collections import Counter

#: Module switch: False leaves the script untouched (the baseline the equality pin compares against).
HOIST_ENABLED = True
#: Shortest chunk (escaped source characters) worth a const; below this the reference costs as much as it saves.
MIN_CHUNK = 40

_PREV_OPENERS = set("(,=:[!&|?{};+-*%<>~^")
_ESC_UNIT = re.compile(r"\\.|[^\\]", re.S)
_REF_NAME = "_PF{}"


def _scan_literals(src: str) -> list[tuple[int, int]]:
    """Spans (start, end-exclusive, quotes included) of single-quoted literals outside templates, comments and regexes."""
    spans: list[tuple[int, int]] = []
    n = len(src)

    def code(i: int, in_template_expr: bool) -> int:
        prev = ""
        depth = 0
        while i < n:
            c = src[i]
            if c == "'":
                j = i + 1
                while j < n and src[j] != "'":
                    j += 2 if src[j] == "\\" else 1
                if not in_template_expr:
                    spans.append((i, j + 1))
                i, prev = j + 1, "'"
            elif c == '"':
                j = i + 1
                while j < n and src[j] != '"':
                    j += 2 if src[j] == "\\" else 1
                i, prev = j + 1, '"'
            elif c == "`":
                i = template(i + 1)
                prev = "`"
            elif c == "/":
                nxt = src[i + 1] if i + 1 < n else ""
                if nxt == "/":
                    k = src.find("\n", i)
                    i = n if k < 0 else k
                elif nxt == "*":
                    k = src.find("*/", i + 2)
                    i = n if k < 0 else k + 2
                elif prev == "" or prev in _PREV_OPENERS or src[max(0, i - 8) : i].rstrip().endswith(("return", "typeof")):
                    j, in_cls = i + 1, False
                    while j < n and (in_cls or src[j] != "/") and src[j] != "\n":
                        if src[j] == "\\":
                            j += 1
                        elif src[j] == "[":
                            in_cls = True
                        elif src[j] == "]":
                            in_cls = False
                        j += 1
                    i, prev = j + 1, "/"
                else:
                    i, prev = i + 1, "/"
            elif c == "{":
                depth += 1
                i, prev = i + 1, c
            elif c == "}":
                if in_template_expr and depth == 0:
                    return i + 1
                depth -= 1
                i, prev = i + 1, c
            else:
                if not c.isspace():
                    prev = c
                i += 1
        return i

    def template(i: int) -> int:
        while i < n:
            c = src[i]
            if c == "\\":
                i += 2
            elif c == "`":
                return i + 1
            elif c == "$" and src[i + 1 : i + 2] == "{":
                i = code(i + 2, True)
            else:
                i += 1
        return i

    code(0, False)
    return spans


def _chunks(content: str) -> list[str]:
    """Split escaped literal content after each '. ' or escaped newline, never inside an escape."""
    out: list[str] = []
    cur: list[str] = []
    units = _ESC_UNIT.findall(content)
    for k, u in enumerate(units):
        cur.append(u)
        if u == "\\n" or (u == " " and k and units[k - 1] == "."):
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def hoist_repeated_fragments(script: str, *, anchor: str) -> str:
    """Return ``script`` with repeated chunks hoisted into `const _PFn` lines.

    The consts go on the lines right after the first line containing ``anchor``, so they precede every use.
    """
    if not HOIST_ENABLED:
        return script
    spans = _scan_literals(script)
    per_literal = [_chunks(script[s + 1 : e - 1]) for s, e in spans]
    counts = Counter(c for chunks in per_literal for c in chunks if len(c) >= MIN_CHUNK)
    names: dict[str, str] = {}
    for chunk, cnt in counts.most_common():
        if cnt < 2:
            continue
        ref = _REF_NAME.format(len(names))
        # Saving: (cnt-1) copies gone minus a ~10-byte reference per use and the const line.
        if (cnt - 1) * len(chunk) - cnt * (len(ref) + 6) - len(ref) - 16 > 0:
            names[chunk] = ref
    if not names:
        return script
    out: list[str] = []
    last = 0
    for (s, e), chunks in zip(spans, per_literal):
        if not any(c in names for c in chunks):
            continue
        pieces: list[str] = []
        buf: list[str] = []
        for c in chunks:
            if c in names:
                if buf:
                    pieces.append("'" + "".join(buf) + "'")
                    buf = []
                pieces.append(names[c])
            else:
                buf.append(c)
        if buf:
            pieces.append("'" + "".join(buf) + "'")
        expr = " + ".join(pieces)
        after = script[e : e + 3].lstrip()
        if after[:1] in (".", "[", "(") or after[:2] == "?.":
            expr = "(" + expr + ")"
        out.append(script[last:s])
        out.append(expr)
        last = e
    out.append(script[last:])
    body = "".join(out)
    defs = "".join(f"const {ref} = '{chunk}';\n" for chunk, ref in names.items())
    at = body.index(anchor)
    eol = body.index("\n", at) + 1
    return body[:eol] + defs + body[eol:]


_DEF_RE = re.compile(r"^const (_PF\d+) = '((?:\\.|[^\\'])*)';\n", re.M)


def resolve_fragment_refs(script: str) -> str:
    """Inverse of the hoist for tests: drop the const lines, inline each reference, fold adjacent literals."""
    defs = dict(_DEF_RE.findall(script))
    body = _DEF_RE.sub("", script)
    for ref in sorted(defs, key=len, reverse=True):
        body = re.sub(rf"\b{ref}\b", lambda _m, r=ref: "'" + defs[r] + "'", body)
    return fold_literals(body)


def fold_literals(script: str) -> str:
    """Merge `'a' + 'b'` into `'ab'` over scanned single-quoted literals only, then unwrap `('x').m`."""
    out: list[str] = []
    last = 0
    spans = _scan_literals(script)
    k = 0
    while k < len(spans):
        s, e = spans[k]
        j = k
        while j + 1 < len(spans) and script[spans[j][1] : spans[j + 1][0]] == " + ":
            j += 1
        if j > k:
            out.append(script[last:s])
            out.append("'" + "".join(script[a + 1 : b - 1] for a, b in spans[k : j + 1]) + "'")
            last = spans[j][1]
        k = j + 1
    out.append(script[last:])
    return _WRAPPED.sub(r"\1", "".join(out))


_WRAPPED = re.compile(r"\(('(?:\\.|[^\\'])*')\)(?=[.\[(]|\?\.)")
