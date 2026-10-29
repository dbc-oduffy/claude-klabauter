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

import json
import re
from bisect import bisect_right
from collections import Counter

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
MIN_JSON = 40
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
                j = i + 1
                while j < n and src[j] != c:
                    j += 2 if src[j] == "\\" else 1
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


def _single_quoted_content(src: str, s: int, e: int) -> str:
    """Literal content re-expressed for single quotes: `\\"` becomes `"` and a bare `'` becomes `\\'`."""
    content = src[s + 1 : e - 1]
    if src[s] == "'":
        return content
    return "".join('"' if u == '\\"' else "\\'" if u == "'" else u for u in _ESC_UNIT.findall(content))


_OPERAND_START_OK = set("(,=:[{;?+")
_OPERAND_END_BAD = set(".[(*/%`")
_COMPOSITE_ROUNDS = 200


class _Tok:
    __slots__ = ("start", "end", "text", "ident", "composite")

    def __init__(self, start: int, end: int, text: str, ident: int, composite: bool = False):
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
        after = body[ch[-1].end :].lstrip()
        ok_end = not after or (after[0] not in _OPERAND_END_BAD and not after.startswith(("?.", "**")))
        ch = ch[(0 if ok_start else 1) : len(ch) - (0 if ok_end else 1)]
        if len(ch) >= 3:
            out.append(ch)
    return out


def _best_composite(chains: list[list[_Tok]], ref: str) -> tuple[tuple[int, ...], list[tuple[int, int]]] | None:
    """The repeated ≥3-operand window with the largest positive byte saving, with its non-overlapping (chain, index) sites."""
    tri: Counter = Counter()
    for ch in chains:
        for i in range(len(ch) - 2):
            tri[(ch[i].ident, ch[i + 1].ident, ch[i + 2].ident)] += 1
    sites: dict[tuple[int, ...], list[tuple[int, int]]] = {}
    for ci, ch in enumerate(chains):
        n = len(ch)
        for i in range(n - 2):
            if tri[(ch[i].ident, ch[i + 1].ident, ch[i + 2].ident)] < 2:
                continue
            j = i + 3
            while True:
                sites.setdefault(tuple(t.ident for t in ch[i:j]), []).append((ci, i))
                if j < n and tri[(ch[j - 2].ident, ch[j - 1].ident, ch[j].ident)] >= 2:
                    j += 1
                else:
                    break
    best = None
    best_gain = 0
    for key, where in sites.items():
        width = len(key)
        kept: list[tuple[int, int]] = []
        last: dict[int, int] = {}
        for ci, i in sorted(where):
            if i >= last.get(ci, -1):
                kept.append((ci, i))
                last[ci] = i + width
        cnt = len(kept)
        if cnt < 2:
            continue
        ci0, i0 = kept[0]
        text_len = sum(len(t.text) for t in chains[ci0][i0 : i0 + width]) + 3 * (width - 1)
        gain = cnt * text_len - cnt * len(ref) - (text_len + len(ref) + 11)
        if gain > best_gain:
            best, best_gain = (key, kept), gain
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
    live = [True] * len(defs)
    for k in range(len(defs) - 1, -1, -1):
        ref = defs[k][0]
        used = re.search(rf"\b{ref}\b", body) or any(
            live[m] and re.search(rf"\b{ref}\b", defs[m][1]) for m in range(k + 1, len(defs))
        )
        live[k] = bool(used)
    return body, [f"const {ref} = {expr};\n" for (ref, expr), keep in zip(defs, live) if keep]


def hoist_repeated_fragments(script: str, *, anchor: str) -> str:
    """Return ``script`` with repeated chunks hoisted into `const _PFn` lines.

    Recurring runs of three or more adjacent constant operands are then hoisted into `const _PFCn` lines.
    The consts go on the lines right after the first line containing ``anchor``, so they precede every use.
    """
    if not HOIST_ENABLED:
        return script
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
    return _hoist_json_values(script, anchor=anchor, first=count)


def _json_end(src: str, i: int) -> int | None:
    """End (exclusive) of the balanced double-quoted-JSON bracket group opening at ``i``, or None for anything else."""
    depth, j, n = 0, i, len(src)
    while j < n:
        c = src[j]
        if c == '"':
            j += 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return j + 1
        elif c in "'`\n":
            return None
        j += 1
    return None


def _json_candidates(script: str) -> dict[str, list[int]]:
    """Source text -> start offsets of each JSON object/array literal of at least MIN_JSON bytes in code position."""
    lits, dead = _scan(script)
    skip = sorted(lits + dead)
    starts = [s for s, _ in skip]
    out: dict[str, list[int]] = {}
    for m in _JSON_OPEN.finditer(script):
        i = m.start()
        k = bisect_right(starts, i) - 1
        if k >= 0 and skip[k][0] <= i < skip[k][1]:
            continue
        j = _json_end(script, i)
        if j is None or j - i < MIN_JSON:
            continue
        text = script[i:j]
        try:
            json.loads(text)
        except ValueError:
            continue
        out.setdefault(text, []).append(i)
    return out


def _hoist_json_values(script: str, *, anchor: str, first: int) -> str:
    """Replace each repeated JSON literal (schemas) with a `_PFn` const reference, largest first, until none pays.

    Trap: a reference shares one object across its uses; nothing the script composes mutates a schema.
    """
    n = first
    while True:
        cands = _json_candidates(script)
        edits: list[tuple[int, int, str]] = []
        defs: list[str] = []
        for text in sorted((t for t, p in cands.items() if len(p) > 1), key=len, reverse=True):
            pos = [p for p in cands[text] if not any(a <= p < b for a, b, _ in edits)]
            name = _REF_NAME.format(n + len(defs))
            if len(pos) < 2 or (len(pos) - 1) * len(text) - (len(pos) + 1) * len(name) - 14 <= 0:
                continue
            defs.append(f"const {name} = {text};\n")
            edits.extend((p, p + len(text), name) for p in pos)
        if not defs:
            return script
        n += len(defs)
        for s, e, name in sorted(edits, reverse=True):
            script = script[:s] + name + script[e:]
        eol = script.index("\n", script.index(anchor)) + 1
        script = script[:eol] + "".join(defs) + script[eol:]


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
    inside = [False] * (len(body) + 1)
    for s, e in lits + dead:
        for k in range(s, e):
            inside[k] = True
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


def resolve_fragment_refs(script: str) -> str:
    """Inverse of the hoist for tests: drop the const lines, inline `_PFC*` then `_PF*` references, fold adjacent literals."""
    composites = dict(_COMPOSITE_RE.findall(script))
    defs = dict(_DEF_RE.findall(script))
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
