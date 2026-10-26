"""Emit repeated `schema: {...}` object literals of a composed Workflow script once.

A composed ask script passes the same result-schema literal to many `agent(...)` calls; the runner
caps the script at a fixed byte size. `dedupe_agent_payloads` turns each byte-identical, constant-only
schema literal that repeats into a `const _SCn = () => (<literal>);` thunk and each site into
`schema: _SCn()`, so every call still builds a fresh object. `resolve_schema_refs` is its exact
textual inverse, for tests.

Trap: the literal is copied verbatim, never converted to JSON; a literal holding any identifier other
than a key, `true`/`false`/`null` or a `_PF*` fragment ref stays inline, because a thunk hoisted to top
level would otherwise capture a different binding.
"""

from __future__ import annotations

import re
from collections import Counter

from coordinator_core.ops.dispatch_emit import prompt_fragments as _pf

_SITE = re.compile(r"\bschema:\s*(?=\{)")
_DEF = re.compile(r"^const (_SC\d+) = \(\) => \((?=\{)", re.M)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_PURE_CODE = re.compile(r"[\s{}\[\]:,+]*")
_LITERAL_IDENTS = {"true", "false", "null"}
_FRAGMENT_REF = re.compile(r"_PFC?\d+")


def _code_mask(src: str) -> bytearray:
    """1 where ``src[i]`` is code, 0 inside strings (quotes included), template text, comments and regexes."""
    n = len(src)
    mask = bytearray(n)

    def code(i: int, in_expr: bool) -> int:
        prev = ""
        depth = 0
        while i < n:
            c = src[i]
            if c in "'\"":
                j = i + 1
                while j < n and src[j] != c:
                    j += 2 if src[j] == "\\" else 1
                i, prev = j + 1, c
                continue
            if c == "`":
                i, prev = template(i + 1), "`"
                continue
            if c == "/":
                nxt = src[i + 1] if i + 1 < n else ""
                if nxt == "/":
                    k = src.find("\n", i)
                    i = n if k < 0 else k
                    continue
                if nxt == "*":
                    k = src.find("*/", i + 2)
                    i = n if k < 0 else k + 2
                    continue
                if prev == "" or prev in _pf._PREV_OPENERS or src[max(0, i - 8) : i].rstrip().endswith(("return", "typeof")):
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
                    continue
            if c == "}" and in_expr and depth == 0:
                return i + 1
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            mask[i] = 1
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
    return mask


def _brace_end(src: str, mask: bytearray, start: int) -> int:
    """End-exclusive index of the object literal opening at ``src[start]``, or -1 when unbalanced."""
    depth = 0
    for i in range(start, len(src)):
        if not mask[i]:
            continue
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _is_constant_literal(text: str, mask: bytearray, start: int) -> bool:
    """True when ``text`` (at ``mask[start:]``) holds only literals, keys and nested object/array literals."""
    code = "".join(c if mask[start + k] else " " for k, c in enumerate(text))
    if "`" in text:
        return False
    code = _NUMBER.sub(" ", code)
    for m in _IDENT.finditer(code):
        word = m.group()
        if word in _LITERAL_IDENTS or _FRAGMENT_REF.fullmatch(word):
            continue
        if not code[m.end() :].lstrip().startswith(":"):
            return False
    return _PURE_CODE.fullmatch(_IDENT.sub(" ", code)) is not None


def dedupe_agent_payloads(script: str, *, anchor: str) -> str:
    """Return ``script`` with repeated constant `schema: {...}` literals emitted once as `_SCn` thunks.

    The thunks go on the lines right after the first line containing ``anchor``. Code-line indentation
    is then stripped from the whole script, whether or not any schema was deduped.
    """
    if not _pf.HOIST_ENABLED or anchor not in script:
        return script
    return strip_code_indentation(_dedupe_schemas(script, anchor))


_INDENT = re.compile(r"^[ \t]+", re.M)


def strip_code_indentation(script: str) -> str:
    """Drop the leading spaces and tabs of every line that starts in code, never inside a template, string or comment."""
    mask = _code_mask(script)
    return _INDENT.sub(lambda m: "" if mask[m.start()] else m.group(), script)


def _dedupe_schemas(script: str, anchor: str) -> str:
    mask = _code_mask(script)
    sites: list[tuple[int, int]] = []
    last_end = 0
    for m in _SITE.finditer(script):
        b = m.end()
        if m.start() < last_end or not mask[m.start()]:
            continue
        e = _brace_end(script, mask, b)
        if e < 0 or not _is_constant_literal(script[b:e], mask, b):
            continue
        sites.append((b, e))
        last_end = e
    counts = Counter(script[b:e] for b, e in sites)
    names: dict[str, str] = {}
    ranked = sorted(counts.items(), key=lambda kv: -(kv[1] - 1) * len(kv[0]))
    for text, cnt in ranked:
        if cnt < 2:
            continue
        ref = f"_SC{len(names)}"
        if cnt * (len(text) - len(ref) - 2) - len(text) - len(ref) - 20 > 0:
            names[text] = ref
    if not names:
        return script
    out: list[str] = []
    last = 0
    for b, e in sites:
        ref = names.get(script[b:e])
        if ref is None:
            continue
        out.append(script[last:b])
        out.append(f"{ref}()")
        last = e
    out.append(script[last:])
    body = "".join(out)
    defs = "".join(f"const {ref} = () => ({text});\n" for text, ref in names.items())
    eol = body.index("\n", body.index(anchor)) + 1
    return body[:eol] + defs + body[eol:]


def resolve_schema_refs(script: str) -> str:
    """Inverse of `dedupe_agent_payloads`: drop the `_SCn` thunk lines and inline each `_SCn()` call."""
    mask = _code_mask(script)
    defs: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for m in _DEF.finditer(script):
        b = m.end()
        e = _brace_end(script, mask, b)
        if e < 0 or script[e : e + 3] != ");\n":
            continue
        defs[m.group(1)] = script[b:e]
        spans.append((m.start(), e + 3))
    out: list[str] = []
    last = 0
    for s, e in spans:
        out.append(script[last:s])
        last = e
    out.append(script[last:])
    body = "".join(out)
    if not defs:
        return body
    return re.sub(r"\b(_SC\d+)\(\)", lambda m: defs.get(m.group(1), m.group(0)), body)
