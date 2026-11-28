"""Hoist repeated static prompt text in a composed Workflow script into top-level consts.

A composed ask script inlines the same sentences in many `agent(...)` prompts; the Workflow runner
caps the script at a fixed byte size. `hoist_repeated_fragments` rewrites each single- or double-quoted
string literal so a sentence-sized chunk that repeats across literals becomes a `_PFn` const reference,
then collapses recurring runs of constant operands into `_PFCn` consts.
The rewrite is concatenation-only, so every run-time string is unchanged. Further passes share the
`_PFn` namespace: adjacent `A + B` terms that repeat merge into one const, and a repeated JSON object or
array literal (a stage schema) becomes one const.

Trap: template literals, comments and regex literals are skipped, never hoisted into; the scanner
decides regex-vs-division from the previous significant character.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right, insort
from collections import Counter
from functools import lru_cache
from itertools import accumulate

#: Module switch: False leaves the script untouched (the baseline the equality pin compares against).
HOIST_ENABLED = True
#: Shortest chunk (escaped source characters) worth a const; below this the reference costs as much as it saves.
MIN_CHUNK = 40

_PREV_OPENERS = set("(,=:[!&|?{};+-*%<>~^")
_ESC_UNIT = re.compile(r"\\.|[^\\]", re.S)
_REF_NAME = "_PF{}"
#: A run of code characters (whitespace included) no scanner branch acts on: consumed in one match.
_PLAIN_RUN = re.compile(r"[^'\"`/{}]+")


def _before(src: str, i: int) -> str:
    """Up to eight characters of ``src[:i].rstrip()``'s tail ('' when only whitespace precedes), without copying the prefix."""
    j = i
    while j > 0 and src[j - 1].isspace():
        j -= 1
    return src[max(0, j - 8) : j]


#: Shortest JSON object/array literal worth a const.
MIN_JSON = 16
_REF_AS_NULL = re.compile(r"\b_PF\d+\b")
_JSON_OPEN = re.compile(r'[\[{]"')


def _scan_literals(src: str) -> list[tuple[int, int]]:
    """Spans (start, end-exclusive, quotes included) of single- and double-quoted literals outside templates, comments and regexes."""
    return _scan(src)[0]


def _scan(src: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(single- and double-quoted literal spans, spans of templates, comments and regexes) of ``src``."""
    spans: list[tuple[int, int]] = []
    dead: list[tuple[int, int]] = []
    n = len(src)

    def code(i: int, in_template_expr: bool) -> int:
        prev = ""
        depth = 0
        while i < n:
            c = src[i]
            if c == "'" or c == '"':
                j = string_end(src, i + 1, c)
                if not in_template_expr:
                    spans.append((i, j + 1))
                i, prev = j + 1, c
            elif c == "`":
                start = i
                i = template(i + 1)
                dead.append((start, i))
                prev = "`"
            elif c == "/":
                nxt = src[i + 1] if i + 1 < n else ""
                if nxt == "/":
                    k = src.find("\n", i)
                    dead.append((i, n if k < 0 else k))
                    i = n if k < 0 else k
                elif nxt == "*":
                    k = src.find("*/", i + 2)
                    dead.append((i, n if k < 0 else k + 2))
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
                    dead.append((i, j + 1))
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
                end = _PLAIN_RUN.match(src, i).end()
                tail = src[i:end].rstrip()
                if tail:
                    prev = tail[-1]
                i = end
        return i

    def template(i: int) -> int:
        while i < n:
            i = _TEMPLATE_PLAIN.match(src, i).end()
            if i >= n:
                break
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
    return spans, dead


_STRING_BODY = {q: re.compile(r"(?:[^%s\\]+|\\.)*" % q, re.S) for q in "'\""}
_TEMPLATE_PLAIN = re.compile(r"[^\\`$]*")


def string_end(src: str, i: int, quote: str) -> int:
    """Index of the closing ``quote`` of the string whose body starts at ``i``; ``len(src)`` when unterminated, one past it when a lone backslash ends ``src``."""
    j = _STRING_BODY[quote].match(src, i).end()
    return len(src) + 1 if j < len(src) and src[j] != quote else j


@lru_cache(maxsize=8192)
def _chunks(content: str) -> tuple[str, ...]:
    """Split escaped literal content after each '. ' or escaped newline, never inside an escape."""
    if not content:
        return ()
    if "\\" not in content and ". " not in content:
        return (content,)
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
    return tuple(out)


_LINE_TERMINATOR = re.compile("[\r\n  ]")
_TERMINATOR_ESCAPE = {"\r": "\\r", "\n": "\\n", " ": "\\u2028", " ": "\\u2029"}


def _escape_terminators(content: str) -> str:
    """``content`` with each raw JS line terminator spelled as its escape; a raw one ends a quoted literal early."""
    return _LINE_TERMINATOR.sub(lambda m: _TERMINATOR_ESCAPE[m.group()], content)


def _escape_literal_terminators(script: str) -> str:
    """``script`` with every raw line terminator inside a quoted literal spelled as its escape (the value is unchanged)."""
    if not _LINE_TERMINATOR.search(script):
        return script
    out: list[str] = []
    last = 0
    for s, e in _scan_literals(script):
        body = script[s:e]
        fixed = _escape_terminators(body)
        if fixed != body:
            out.append(script[last:s])
            out.append(fixed)
            last = e
    out.append(script[last:])
    return "".join(out)


def _single_quoted_content(src: str, s: int, e: int) -> str:
    """Literal content re-expressed for single quotes: `\\"` becomes `"`, a bare `'` becomes `\\'`, a raw line terminator its escape."""
    content = _escape_terminators(src[s + 1 : e - 1])
    if src[s] == "'":
        return content
    return "".join('"' if u == '\\"' else "\\'" if u == "'" else u for u in _ESC_UNIT.findall(content))


_OPERAND_START_OK = set("(,=:[{;?+")
_OPERAND_END_BAD = set(".[(*/%`")
_COMPOSITE_ROUNDS = 200
_FAR = 1 << 62
_COMPOSITE_REF = re.compile(r"\b_PFC\d+\b")


class _Tok:
    __slots__ = ("start", "end", "text", "ident", "composite")

    def __init__(self, start: int, end: int, text: str, ident: int, composite: bool = False) -> None:
        self.start, self.end, self.text, self.ident, self.composite = start, end, text, ident, composite


def _constant_chains(body: str, ids: dict[str, int]) -> list[list[_Tok]]:
    """Maximal ` + `-joined runs of `_PFn` refs and single-quoted literals, trimmed so no operand binds tighter than `+`."""
    lits = [(s, e) for s, e in _scan_literals(body) if body[s] == "'"]
    toks = [(s, e) for s, e in lits]
    for m in re.finditer(r"\b_PF\d+\b", body):
        toks.append((m.start(), m.end()))
    toks.sort()
    chains: list[list[_Tok]] = []
    prev_end = -1
    for s, e in toks:
        text = body[s:e]
        tok = _Tok(s, e, text, ids.setdefault(text, len(ids)))
        if chains and body[prev_end:s] == " + ":
            chains[-1].append(tok)
        else:
            chains.append([tok])
        prev_end = e
    out: list[list[_Tok]] = []
    for ch in chains:
        before = _before(body, ch[0].start)
        ok_start = (
            not before
            or before[-1] in _OPERAND_START_OK
            or before.endswith("return")
            or before.endswith("=>")
        )
        k = ch[-1].end
        while k < len(body) and body[k].isspace():
            k += 1
        after = body[k : k + 2]
        ok_end = not after or (after[0] not in _OPERAND_END_BAD and not after.startswith(("?.", "**")))
        ch = ch[(0 if ok_start else 1) : len(ch) - (0 if ok_end else 1)]
        if len(ch) >= 3:
            out.append(ch)
    return out


def _best_composite(chains: list[list[_Tok]], ref: str) -> tuple[tuple[int, ...], list[tuple[int, int]]] | None:
    """The repeated ≥3-operand window with the largest positive byte saving, with its non-overlapping (chain, index) sites."""
    idents = [[t.ident for t in ch] for ch in chains]
    prefix = [list(accumulate((len(t.text) for t in ch), initial=0)) for ch in chains]
    tri: Counter = Counter()
    for ids in idents:
        tri.update(zip(ids, ids[1:], ids[2:]))
    # A window is keyed by an interned id grown one operand at a time: extending it is O(1), not a slice.
    intern: dict[tuple[int, int], int] = {}
    sites: dict[int, list[tuple[int, int]]] = {}
    widths: dict[int, int] = {}
    for ci, ids in enumerate(idents):
        n = len(ids)
        for i in range(n - 2):
            if tri[(ids[i], ids[i + 1], ids[i + 2])] < 2:
                continue
            k = intern.setdefault((-1, ids[i]), len(intern))
            k = intern.setdefault((k, ids[i + 1]), len(intern))
            j = i + 3
            k = intern.setdefault((k, ids[i + 2]), len(intern))
            while True:
                where = sites.get(k)
                if where is None:
                    sites[k] = where = []
                    widths[k] = j - i
                where.append((ci, i))
                if j < n and tri[(ids[j - 2], ids[j - 1], ids[j])] >= 2:
                    step = (k, ids[j])
                    k = intern.get(step)
                    if k is None:
                        k = intern[step] = len(intern)
                    j += 1
                else:
                    break
    best = None
    best_gain = 0
    for k, where in sites.items():
        if len(where) < 2:
            continue
        width = widths[k]
        kept: list[tuple[int, int]] = []
        last: dict[int, int] = {}
        for ci, i in where:
            if i >= last.get(ci, -1):
                kept.append((ci, i))
                last[ci] = i + width
        cnt = len(kept)
        if cnt < 2:
            continue
        ci0, i0 = kept[0]
        pre = prefix[ci0]
        text_len = pre[i0 + width] - pre[i0] + 3 * (width - 1)
        gain = cnt * text_len - cnt * len(ref) - (text_len + len(ref) + 11)
        if gain > best_gain:
            best, best_gain = (tuple(idents[ci0][i0 : i0 + width]), kept), gain
    return best


def _hoist_constant_runs(body: str) -> tuple[str, list[str]]:
    """Replace recurring runs of constant operands with `_PFCn` refs; returns the body and the const lines."""
    ids: dict[str, int] = {}
    chains = _constant_chains(body, ids)
    defs: list[tuple[str, str]] = []
    for _ in range(_COMPOSITE_ROUNDS):
        ref = f"_PFC{len(defs)}"
        found = _best_composite(chains, ref)
        if found is None:
            break
        key, kept = found
        ci0, i0 = kept[0]
        defs.append((ref, " + ".join(t.text for t in chains[ci0][i0 : i0 + len(key)])))
        ident = ids.setdefault(ref, len(ids))
        for ci, i in sorted(kept, reverse=True):
            ch = chains[ci]
            ch[i : i + len(key)] = [_Tok(ch[i].start, ch[i + len(key) - 1].end, ref, ident, True)]
    if not defs:
        return body, []
    edits = sorted((t.start, t.end, t.text) for ch in chains for t in ch if t.composite)
    out: list[str] = []
    last = 0
    for s, e, ref in edits:
        out.append(body[last:s])
        out.append(ref)
        last = e
    out.append(body[last:])
    body = "".join(out)
    in_body = set(_COMPOSITE_REF.findall(body))
    in_defs = [set(_COMPOSITE_REF.findall(expr)) for _, expr in defs]
    live = [True] * len(defs)
    for k in range(len(defs) - 1, -1, -1):
        ref = defs[k][0]
        live[k] = ref in in_body or any(live[m] and ref in in_defs[m] for m in range(k + 1, len(defs)))
    return body, [f"const {ref} = {expr};\n" for (ref, expr), keep in zip(defs, live) if keep]


_HOIST_MEMO: dict[tuple, str] = {}
_HOIST_MEMO_MAX = 4


def hoist_repeated_fragments(script: str, *, anchor: str) -> str:
    """Return ``script`` with repeated chunks hoisted into `const _PFn` lines.

    Recurring runs of three or more adjacent constant operands are then hoisted into `const _PFCn` lines.
    The consts go on the lines right after the first line containing ``anchor``, so they precede every use.

    The rewrite is a pure function of ``script``, ``anchor`` and the module knobs, so a refire that
    composes the same script again reads the result from a small per-process memo (oldest out).
    """
    if not HOIST_ENABLED:
        return script
    key = (hashlib.sha256(script.encode("utf-8", "surrogatepass")).digest(), anchor, MIN_CHUNK, MIN_JSON)
    hit = _HOIST_MEMO.get(key)
    if hit is None:
        hit = _hoist_repeated_fragments(script, anchor=anchor)
        while len(_HOIST_MEMO) >= _HOIST_MEMO_MAX:
            _HOIST_MEMO.pop(next(iter(_HOIST_MEMO)))
        _HOIST_MEMO[key] = hit
    return hit


def _hoist_repeated_fragments(script: str, *, anchor: str) -> str:
    script = _escape_literal_terminators(script)
    spans = _scan_literals(script)
    per_literal = [_chunks(_single_quoted_content(script, s, e)) for s, e in spans]
    counts = Counter(c for chunks in per_literal for c in chunks if len(c) >= MIN_CHUNK)
    names: dict[str, str] = {}
    for chunk, cnt in counts.most_common():
        if cnt < 2:
            continue
        ref = _REF_NAME.format(len(names))
        # Saving: (cnt-1) copies gone minus a ~10-byte reference per use and the const line.
        if (cnt - 1) * len(chunk) - cnt * (len(ref) + 6) - len(ref) - 16 > 0:
            names[chunk] = ref
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
    body, composite_defs = _hoist_constant_runs("".join(out))
    defs = "".join(f"const {ref} = '{chunk}';\n" for chunk, ref in names.items()) + "".join(composite_defs)
    body, run_defs, count = _hoist_runs(body, len(names))
    defs += run_defs
    script = body
    if defs:
        eol = body.index("\n", body.index(anchor)) + 1
        script = body[:eol] + defs + body[eol:]
    return _renumber_high_refs(_hoist_substrings(_hoist_json_values(script, anchor=anchor, first=count), anchor=anchor))


#: A double-quoted string, a lone `"` (unterminated), a char that ends a JSON literal, or a bracket.
_JSON_TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|["\'`\n]|[\[\]{}]', re.S)


def _json_end(src: str, i: int) -> int | None:
    """End (exclusive) of the balanced double-quoted-JSON bracket group opening at ``i``, or None for anything else."""
    depth = 0
    for m in _JSON_TOKEN.finditer(src, i):
        c = m.group()
        if c[0] == '"':
            if len(c) == 1:
                return None
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return m.end()
        else:
            return None
    return None


def _json_candidates(
    script: str, *, repeated_only: bool = False, allow_refs: bool = False, scan: list | None = None
) -> dict[str, list[int]]:
    """Source text -> start offsets of each JSON object/array literal of at least MIN_JSON bytes in code position.

    ``repeated_only`` drops texts that occur once as raw text before the literal scan and the JSON parse:
    a text that never repeats cannot be hoisted, and most literals never do.

    ``scan`` is a one-slot cache of ``_scan(script)``: filled here when empty, so a caller that keeps it current across
    edits (``_remap_scan``) pays the scan once.
    """
    found: list[tuple[int, str]] = []
    count: Counter = Counter()
    for m in _JSON_OPEN.finditer(script):
        i = m.start()
        j = _json_end(script, i)
        if j is None or j - i < MIN_JSON:
            continue
        text = script[i:j]
        found.append((i, text))
        count[text] += 1
    if repeated_only:
        found = [(i, t) for i, t in found if count[t] > 1]
    if not found:
        return {}
    if scan is None:
        scan = [None]
    if scan[0] is None:
        scan[0] = _scan(script)
    lits, dead = scan[0]
    skip = sorted(lits + dead)
    starts = [s for s, _ in skip]
    valid: dict[str, bool] = {}
    out: dict[str, list[int]] = {}
    for i, text in found:
        k = bisect_right(starts, i) - 1
        if k >= 0 and skip[k][0] <= i < skip[k][1]:
            continue
        ok = valid.get(text)
        if ok is None:
            try:
                json.loads(_REF_AS_NULL.sub("null", text) if allow_refs else text)
                ok = True
            except ValueError:
                ok = False
            valid[text] = ok
        if ok:
            out.setdefault(text, []).append(i)
    return out


#: Rounds of the JSON-literal hoist; a later round hoists values nested in an earlier round's defs.
_JSON_ROUNDS = 8


def _remap_scan(
    scan: tuple[list[tuple[int, int]], list[tuple[int, int]]],
    edits: list[tuple[int, int, str]],
    at: int,
    defs_text: str,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """``_scan`` of a script after ``edits`` (sorted, non-overlapping literal replacements) and the insert of ``defs_text`` at ``at``, without rescanning it.

    Spans inside a replaced literal vanish with it; spans after an edit shift by its length change; the inserted
    defs are the only text scanned fresh. ``at`` is in post-edit coordinates and is a line start outside any span.
    """
    starts = [s for s, _, _ in edits]
    cum = [0]
    for s, e, name in edits:
        cum.append(cum[-1] + len(name) - (e - s))
    shift = len(defs_text)
    new_lits, new_dead = _scan(defs_text)

    def move(spans: list[tuple[int, int]], fresh: list[tuple[int, int]]) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for a, b in spans:
            k = bisect_right(starts, a) - 1
            if k >= 0 and a < edits[k][1]:
                continue
            d = cum[k + 1]
            a, b = a + d, b + d
            out.append((a + shift, b + shift) if a >= at else (a, b))
        out.extend((a + at, b + at) for a, b in fresh)
        return out

    return move(scan[0], new_lits), move(scan[1], new_dead)


def _hoist_json_values(script: str, *, anchor: str, first: int) -> str:
    """Replace each repeated JSON literal (schemas) with a `_PFn` const reference, largest first, until none pays.

    Trap: a reference shares one object across its uses; nothing the script composes mutates a schema.
    """
    n = first
    script = _resolve_aliases(script)
    # A schema may now name string consts the chunk hoist declared, so its defs land below that block.
    block = 0
    at0 = script.index("\n", script.index(anchor)) + 1
    while script.startswith("const _PF", at0):
        at0 = script.index("\n", at0) + 1
        block += 1
    scan: list = [None]
    for _round in range(_JSON_ROUNDS):
        cands = _json_candidates(script, repeated_only=True, allow_refs=True, scan=scan)
        edits: list[tuple[int, int, str]] = []
        taken: list[tuple[int, int]] = []
        defs: list[str] = []
        for text in sorted((t for t, p in cands.items() if len(p) > 1), key=len, reverse=True):
            pos = []
            for p in cands[text]:
                k = bisect_right(taken, (p, _FAR)) - 1
                if k < 0 or taken[k][1] <= p:
                    pos.append(p)
            name = _REF_NAME.format(n + len(defs))
            if len(pos) < 2 or (len(pos) - 1) * len(text) - (len(pos) + 1) * len(name) - 14 <= 0:
                continue
            defs.append(f"const {name} = {text};\n")
            edits.extend((p, p + len(text), name) for p in pos)
            for p in pos:
                insort(taken, (p, p + len(text)))
        if not defs:
            return _resolve_aliases(script)
        n += len(defs)
        pieces: list[str] = []
        last = 0
        edits.sort()
        for s, e, name in edits:
            pieces.append(script[last:s])
            pieces.append(name)
            last = e
        pieces.append(script[last:])
        script = "".join(pieces)
        # A later round hoists values nested in an earlier round's defs, so its defs go above them.
        at = script.index("\n", script.index(anchor)) + 1
        for _ in range(block):
            at = script.index("\n", at) + 1
        defs_text = "".join(defs)
        script = script[:at] + defs_text + script[at:]
        scan[0] = _remap_scan(scan[0], edits, at, defs_text) if scan[0] is not None else None
    return _resolve_aliases(script)


_TOKEN_RE = re.compile(r"\b_PF\d+\b")
_RUN_PREV = set("(,=:?[{&|;+}>")


def _hoist_runs(body: str, first: int) -> tuple[str, str, int]:
    """Merge adjacent ``A + B`` string terms that repeat into `const _PFn = A + B;` lines (pair merging, repeated).

    Terms are `_PFn` references and single-quoted literals outside templates, comments and regexes;
    only terms joined by exactly ` + ` form a run. Names count up from ``first``. Returns the rewritten
    body, the const lines and the next free name index.

    Trap: each merge recounts every pair, so cost grows with const count x term count; the composed-script
    size-budget test runs this on every shape but asserts bytes, not time.
    """
    lits, dead = _scan(body)
    inside = bytearray(len(body) + 1)
    for s, e in lits + dead:
        inside[s:e] = b"\x01" * (min(e, len(inside)) - s)
    toks = [(s, e) for s, e in lits] + [m.span() for m in _TOKEN_RE.finditer(body) if not inside[m.start()]]
    toks.sort()
    runs: list[list[tuple[int, int]]] = []
    for t in toks:
        if runs and body[runs[-1][-1][1] : t[0]] == " + ":
            runs[-1].append(t)
        else:
            runs.append([t])
    seqs: list[list[str]] = []
    spans: list[tuple[int, int]] = []
    for run in runs:
        head = _before(body, run[0][0])
        tail = body[run[-1][1] : run[-1][1] + 3].lstrip()
        if len(run) < 2 or (head and head[-1] not in _RUN_PREV) or tail[:1] in (".", "[", "(", "*", "/", "%") or tail[:2] == "?.":
            continue
        seqs.append([body[a:b] for a, b in run])
        spans.append((run[0][0], run[-1][1]))
    defs: list[str] = []
    while True:
        where: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for si, seq in enumerate(seqs):
            for k in range(len(seq) - 1):
                where.setdefault((seq[k], seq[k + 1]), []).append((si, k))
        best, gain = None, 0
        for (a, b), occ in where.items():
            if len(occ) < 2:
                continue
            ext = [a, b]
            while a != b and all(k + len(ext) < len(seqs[si]) for si, k in occ):
                nxt = {seqs[si][k + len(ext)] for si, k in occ}
                if len(nxt) != 1:
                    break
                ext.append(nxt.pop())
            cnt = len(occ)
            width = sum(map(len, ext)) + 3 * (len(ext) - 1)
            g = cnt * (width - 6) - (width + 22)
            if g > gain:
                best, gain = tuple(ext), g
        if best is None:
            break
        name = _REF_NAME.format(first + len(defs))
        defs.append(f"const {name} = {' + '.join(best)};\n")
        for seq in seqs:
            k = 0
            while k <= len(seq) - len(best):
                if tuple(seq[k : k + len(best)]) == best:
                    seq[k : k + len(best)] = [name]
                else:
                    k += 1
    if not defs:
        return body, "", first
    out: list[str] = []
    last = 0
    for (s, e), seq in zip(spans, seqs):
        out.append(body[last:s])
        out.append(" + ".join(seq))
        last = e
    out.append(body[last:])
    return "".join(out), "".join(defs), first + len(defs)


#: One const line of any hoist kind: a literal chunk, a `+` run of references and literals, or a JSON literal.
_DEF_RE = re.compile(r"^const (_PF\d+) = ([^\n]*);\n", re.M)
_REF = re.compile(r"\b_PF\d+\b")
_OPERAND = r"(?:_PFC?\d+|'(?:\\.|[^\\'])*')"
_COMPOSITE_RE = re.compile(rf"^const (_PFC\d+) = ({_OPERAND}(?: \+ {_OPERAND})*);\n", re.M)
_PLAIN_TEMPLATE = re.compile(r"^`([^`$\\'\n]*)`$")


def resolve_fragment_refs(script: str) -> str:
    """Inverse of the hoist for tests: drop the const lines, inline `_PFC*` then `_PF*` references, fold adjacent literals."""
    composites = dict(_COMPOSITE_RE.findall(script))
    # A plain template-literal value (the sidecar_dir alias, kept out of the hoist's reach) inlines as the
    # single-quoted literal it replaced, so the fold below rejoins it.
    defs = {k: _PLAIN_TEMPLATE.sub(r"'\1'", v) for k, v in _DEF_RE.findall(script)}
    body = _DEF_RE.sub("", _COMPOSITE_RE.sub("", script))
    for ref in sorted(composites, key=lambda r: int(r[4:]), reverse=True):
        body = re.sub(rf"\b{ref}\b", lambda _m, r=ref: composites[r], body)
    while _REF.search(body):
        body = _REF.sub(lambda m: defs[m.group()], body)
    return fold_literals(body)


def fold_literals(script: str) -> str:
    """Merge `'a' + 'b'` into `'ab'` over scanned literals, normalising each to single quotes, then unwrap `('x').m`."""
    out: list[str] = []
    last = 0
    # A double-quoted key or value inside a code-position JSON literal is JSON, not a JS string to normalise.
    json_spans = [(i, i + len(t)) for t, starts in _json_candidates(script).items() for i in starts]
    spans = [(a, b) for a, b in _scan_literals(script) if not any(x <= a < y for x, y in json_spans)]
    k = 0
    while k < len(spans):
        s, e = spans[k]
        j = k
        while j + 1 < len(spans) and script[spans[j][1] : spans[j + 1][0]] == " + ":
            j += 1
        if j > k or script[s] == '"':
            out.append(script[last:s])
            out.append("'" + "".join(_single_quoted_content(script, a, b) for a, b in spans[k : j + 1]) + "'")
            last = spans[j][1]
        k = j + 1
    out.append(script[last:])
    return _WRAPPED.sub(r"\1", "".join(out))


_WRAPPED = re.compile(r"\(('(?:\\.|[^\\'])*')\)(?=[.\[(]|\?\.)")


#: Shortest repeated stretch of literal text worth a const; a use costs a `' + _PFn + '` splice.
MIN_SUBSTRING = 20
_SUBSTRING_ROUNDS = 1
#: Net bytes a stretch must save before it becomes a const.
_SUBSTRING_MIN_SAVING = 10
#: An escape whose interior a splice must never fall in.
_ESCAPE_SPAN = re.compile(r"\\(?:u\{[^}]*\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_MARK_OPEN, _MARK_CLOSE, _LIT_SEP = "\x01", "\x02", "\x00"
_MARKERS = re.compile("[\x00-\x02]")
_WORD_START = re.compile(r"(?<![A-Za-z0-9_])(?=[A-Za-z0-9_])|(?<=[\x00-\x02])")
_MARKED = re.compile("\x01(\\d+)\x02")


def _hoist_substrings(script: str, *, anchor: str) -> str:
    """Hoist stretches of single-quoted literal text that two or more literals share, whatever the sentence boundaries.

    The sentence-chunk hoist misses a repeat whose neighbours differ (a shell command embedded in prose, a
    schema description); this pass finds the longest repeated stretches from word boundaries and cuts each to a
    `_PFn` string const. Concatenation only, so every run-time string is unchanged.

    Trap: a splice never lands inside an escape sequence, and a literal followed by `:` (an object key or a
    `case`) is left whole because `'a' + b` is not a key.
    """
    # Literals above the anchor line run before the consts this pass declares below it, so they stay whole.
    first = script.index("\n", script.index(anchor)) + 1
    spans = [
        (a, b)
        for a, b in _scan_literals(script)
        if a >= first and script[a] == "'" and script[b : b + 4].lstrip()[:1] != ":"
    ]
    text = _LIT_SEP.join(_single_quoted_content(script, a, b) for a, b in spans)
    used = {int(n) for n in re.findall(r"\b_PF(\d+)\b", script)}
    names: dict[int, str] = {}
    nxt = 0
    for _ in range(_SUBSTRING_ROUNDS):
        banned = bytearray(len(text) + 1)
        for m in _ESCAPE_SPAN.finditer(text):
            for k in range(m.start() + 1, m.end()):
                banned[k] = 1
        # A window is usable when it starts on an escape boundary and holds no marker or literal separator.
        usable = bytearray(len(text) + 1)
        usable[: len(text)] = b"" * len(text)
        for m in _MARKERS.finditer(text):
            lo = max(0, m.start() - MIN_SUBSTRING + 1)
            usable[lo : m.start() + 1] = bytes(m.start() + 1 - lo)
        index: dict[str, list[int]] = {}
        # Windows start at a word boundary: a repeat is found from its first word, and the other starts only re-find it.
        for m in _WORD_START.finditer(text, 0, len(text) - MIN_SUBSTRING):
            i = m.start()
            if usable[i] and not banned[i]:
                index.setdefault(text[i : i + MIN_SUBSTRING], []).append(i)
        stretches: dict[str, int] = {}
        for w, at in index.items():
            # A periodic window (a run of one character, a rule of dashes) is filler, not shared text.
            if len(at) < 2 or (w + w).find(w, 1) <= MIN_SUBSTRING // 2:
                continue
            i, j, n = at[0], at[1], MIN_SUBSTRING
            while j + n < len(text) and i + n < j and text[i + n] == text[j + n] and text[i + n] not in _MARK_OPEN + _MARK_CLOSE + _LIT_SEP:
                n += 1
            while banned[i + n] and n > MIN_SUBSTRING:
                n -= 1
            if not banned[i + n]:
                stretches[text[i : i + n]] = len(at)
        picked = False
        for sub in sorted(stretches, key=lambda t: -(stretches[t] - 1) * len(t)):
            if (stretches[sub] - 1) * len(sub) < 2 * _SUBSTRING_MIN_SAVING:
                break
            count = text.count(sub)
            while nxt in used:
                nxt += 1
            ref = _REF_NAME.format(nxt)
            if count >= 2 and (count - 1) * len(sub) - count * (len(ref) + 6) - len(ref) - 16 > _SUBSTRING_MIN_SAVING:
                text = text.replace(sub, f"{_MARK_OPEN}{nxt}{_MARK_CLOSE}")
                names[nxt] = sub
                used.add(nxt)
                picked = True
        if not picked:
            break
    if not names:
        return script
    pieces_of = text.split(_LIT_SEP)
    def _expr(content: str) -> str:
        parts: list[str] = []
        pos = 0
        for m in _MARKED.finditer(content):
            if m.start() > pos:
                parts.append("'" + content[pos : m.start()] + "'")
            parts.append(_REF_NAME.format(int(m.group(1))))
            pos = m.end()
        if pos < len(content):
            parts.append("'" + content[pos:] + "'")
        return " + ".join(parts)

    out: list[str] = []
    last = 0
    for (s, e), content in zip(spans, pieces_of):
        if _MARK_OPEN not in content:
            continue
        expr = _expr(content)
        after = script[e : e + 3].lstrip()
        if after[:1] in (".", "[", "(") or after[:2] == "?.":
            expr = "(" + expr + ")"
        out.append(script[last:s])
        out.append(expr)
        last = e
    out.append(script[last:])
    body = "".join(out)
    # A stretch may hold a reference to a later-picked one, so declare in reverse pick order.
    eol = body.index("\n", body.index(anchor)) + 1
    defs = "".join(f"const {_REF_NAME.format(n)} = '{sub}';\n" for n, sub in names.items())
    return body[:eol] + defs + body[eol:]


def _renumber_high_refs(script: str) -> str:
    """Rename each `_PFn` whose number has four or more digits (the composer's pre-hoist operands) to the smallest free number, most-used first.

    A shorter name is paid at every use; the rename is a pure token swap, so no run-time value changes.
    """
    nums = re.findall(r"\b_PF(\d+)\b", script)
    counts = Counter(n for n in nums if len(n) >= 4)
    if not counts:
        return script
    used = {int(n) for n in set(nums)}
    plan: dict[str, str] = {}
    free = 0
    for old, _ in counts.most_common():
        while free in used:
            free += 1
        plan[old] = str(free)
        used.add(free)
    high = re.compile(r"\b_PF(" + "|".join(sorted(plan, key=len, reverse=True)) + r")\b")
    return high.sub(lambda m: "_PF" + plan[m.group(1)], script)


_ALIAS_DEF = re.compile(r"^const (_PF\w+) = (_PF\w+);\n", re.M)


def _resolve_aliases(script: str) -> str:
    """Drop each `const _PFa = _PFb;` line and read `_PFb` at its uses.

    An alias is a second name for a const declared above it, so every use after it sees the same value; the rename
    also lets two values that differed only by alias name compare equal to the hoists that run after.
    """
    target = {m.group(1): m.group(2) for m in _ALIAS_DEF.finditer(script)}
    if not target:
        return script

    def final(name: str) -> str:
        while name in target:
            name = target[name]
        return name

    names = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, target), key=len, reverse=True)) + r")\b")
    return names.sub(lambda m: final(m.group(0)), _ALIAS_DEF.sub("", script))
