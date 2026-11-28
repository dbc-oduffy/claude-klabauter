"""Stage the large static runs of a composed ask prompt as record-dir files the agent reads by path.

`stage` records one body into the caller's sink and returns the JS literal that stands in for it;
`stage_literal_runs` finds the bodies inside a prompt expression. The composed script holds the
pointer, the engine writes the sink to `<record_dir>/prompts/<name>.md` (op.py), and the agent
receives the same text through Read. Composition stays pure: nothing here touches disk.

Trap: a run is staged only when every escape in it is one this module decodes; any other escape
leaves the run inline rather than guessing its text.
"""

from __future__ import annotations

import functools
import re
from typing import Callable

from coordinator_core.ops.dispatch_emit.prompt_fragments import _scan, _scan_literals, string_end
from coordinator_core.ops.workflow_fire.worker_preamble import bind_engine_commands, needs_binding
from coordinator_core.ops.workflow_scaffold import _js_string_literal

#: Directory under the record dir that holds staged prompt bodies.
PROMPTS_DIR = "prompts"
#: Smallest decoded run (UTF-8 bytes) worth a pointer; below this the pointer costs as much as it saves.
MIN_STAGE_BYTES = 600

_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r"}
_ESC = re.compile(r"\\(.)", re.S)
_JOIN = " + "

Stager = Callable[[str, str], str]


def stage(name: str, text: str, sink: dict[str, str], *, record_dir: str) -> str:
    """Record ``text`` as ``name`` in ``sink`` and return the JS literal pointing at its record-dir file.

    ``record_dir`` is the pointer's path prefix and must be absolute, so a leg whose cwd is not the
    repo holding the record dir (a cross-repo ask) still resolves it. A name already recorded with
    other text is refused.

    The body is bound to the engine interpreter here, the one point every staged leg prompt passes: a
    committed composer keeps the portable `python -m coordinator_core...` form, but the agent reading
    the file may not have that `python` or an importable `coordinator_core`.
    """
    text = bind_engine_commands(text)
    if sink.get(name, text) != text:
        raise ValueError(f"staged prompt {name!r} already recorded with different text")
    sink[name] = text
    return _js_string_literal(
        f"Read {record_dir}/{PROMPTS_DIR}/{name}.md in full and follow it as the rest of this brief.\n"
    )


@functools.lru_cache(maxsize=4096)
def _decode(content: str) -> str | None:
    """The text a single-quoted literal's escaped ``content`` denotes, or None on an escape not decoded."""
    for m in _ESC.finditer(content):
        if m.group(1) not in _ESCAPES:
            return None
    return _ESC.sub(lambda m: _ESCAPES[m.group(1)], content)


def _code_span_edges(js: str, run: list[tuple[int, int]], text: str) -> tuple[int, int]:
    """The ``[lead, tail)`` slice of ``text`` safe to stage without cutting a dynamic instruction's line or code
    span: an odd backtick count opens or closes a code span around a neighbouring JS operand, and a balanced run
    ending mid-line or wrapping dynamic CLI tokens (`[`, `]`, `--`) before/after an operand keeps that boundary
    line inline so the staged file never splits a command or sentence across the operand."""
    lead, tail = 0, len(text)
    odd_ticks = text.count("`") % 2
    if js[max(0, run[0][0] - len(_JOIN)) : run[0][0]] == _JOIN:
        first_line = text.split("\n", 1)[0]
        if odd_ticks or first_line.lstrip().startswith(("]", "--")):
            eol = text.find("\n", text.index("`") if odd_ticks else 0)
            lead = len(text) if eol < 0 else eol + 1
    if js[run[-1][1] : run[-1][1] + len(_JOIN)] == _JOIN:
        last_line = text.rsplit("\n", 1)[-1]
        if odd_ticks or "[" in last_line or "--" in last_line:
            tail = text.rfind("\n", 0, text.rindex("`") if odd_ticks else len(text)) + 1
        elif not text.endswith("\n") and "\n" in text:
            tail = text.rfind("\n") + 1
    return lead, max(lead, tail)


def _literal_runs(js: str, literals: list[tuple[int, int]] | None = None) -> list[list[tuple[int, int]]]:
    """Runs of single-quoted literals joined by ` + `, as span lists."""
    spans = [(s, e) for s, e in (_scan_literals(js) if literals is None else literals) if js[s] == "'"]
    runs: list[list[tuple[int, int]]] = []
    for span in spans:
        if runs and js[runs[-1][-1][1] : span[0]] == _JOIN:
            runs[-1].append(span)
        else:
            runs.append([span])
    return runs


def bind_script_literals(js: str) -> str:
    """``js`` with each literal run that names a portable engine command rewritten bound (`bind_engine_commands`).

    For a script delivered whole to an agent with no staging (plan route, re-verify, resume). The run is
    re-encoded as one literal, since the bound form carries quotes on Windows. Trap: a run with an escape
    `_decode` does not know stays as written, bare; so does a command inside a template literal.
    """
    out: list[str] = []
    last = 0
    for run in _literal_runs(js):
        texts = [_decode(js[s + 1 : e - 1]) for s, e in run]
        if any(t is None for t in texts):
            continue
        text = "".join(texts)  # type: ignore[arg-type]
        if not needs_binding(text):
            continue
        out.append(js[last : run[0][0]])
        out.append(_js_string_literal(bind_engine_commands(text)))
        last = run[-1][1]
    out.append(js[last:])
    return "".join(out)


_TEMPLATE_ESCAPES = {"\\": "\\", "`": "`", "$": "$", "{": "{", "n": "\n", "t": "\t"}


def _template_decode(raw: str) -> str | None:
    """The text a template literal's static ``raw`` segment denotes, or None on a raw CR or an escape not decoded."""
    if "\r" in raw:
        return None
    for m in _ESC.finditer(raw):
        if m.group(1) not in _TEMPLATE_ESCAPES:
            return None
    return _ESC.sub(lambda m: _TEMPLATE_ESCAPES[m.group(1)], raw)


def _template_encode(text: str) -> str:
    """``text`` as the raw body of a template literal."""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _skip_template(js: str, i: int) -> int:
    """Index just past the template literal whose body starts at ``i``, or -1 when unterminated."""
    n = len(js)
    while i < n:
        c = js[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and js.startswith("{", i + 1):
            i = _skip_expr(js, i + 2)
            if i < 0:
                return -1
        else:
            i += 1
    return -1


def _skip_expr(js: str, i: int) -> int:
    """Index just past the `}` closing the `${` expression whose body starts at ``i``, or -1 when unmatched."""
    n, depth = len(js), 0
    while i < n:
        c = js[i]
        if c in "'\"":
            i = string_end(js, i + 1, c) + 1
        elif c == "`":
            i = _skip_template(js, i + 1)
            if i < 0:
                return -1
        elif c == "{":
            depth, i = depth + 1, i + 1
        elif c == "}":
            if depth == 0:
                return i + 1
            depth, i = depth - 1, i + 1
        else:
            i += 1
    return -1


def _template_segments(js: str, start: int, end: int) -> list[tuple[int, int]] | None:
    """Raw spans of the static segments of the template ``js[start:end]``, split at each `${...}`; None when unparseable."""
    segs: list[tuple[int, int]] = []
    i, seg_a, stop = start + 1, start + 1, end - 1
    while i < stop:
        c = js[i]
        if c == "\\":
            i += 2
        elif c == "$" and js.startswith("{", i + 1):
            segs.append((seg_a, i))
            i = _skip_expr(js, i + 2)
            if i < 0 or i > stop:
                return None
            seg_a = i
        else:
            i += 1
    segs.append((seg_a, stop))
    return segs


def _top_level_templates(dead: list[tuple[int, int]], js: str) -> list[tuple[int, int]]:
    """Template literal spans not nested inside another template's `${}`, in script order."""
    out: list[tuple[int, int]] = []
    reach = 0
    for s, e in sorted(dead):
        if s < reach:
            continue
        reach = e
        if js[s] == "`" and not _is_tagged(js, s):
            out.append((s, e))
    return out


_KEYWORDS_BEFORE_TEMPLATE = frozenset(
    {"return", "typeof", "case", "yield", "await", "in", "of", "else", "do", "void", "delete", "new", "throw", "instanceof"}
)


def _is_tagged(js: str, start: int) -> bool:
    """True when the backtick at ``start`` follows an expression, making it a tagged template whose
    cooked/raw semantics splicing ``${pointer}`` would change."""
    i = start
    while i > 0 and js[i - 1].isspace():
        i -= 1
    if i == 0:
        return False
    prev = js[i - 1]
    if prev in ")]":
        return True
    if prev.isalnum() or prev in "_$":
        j = i
        while j > 0 and (js[j - 1].isalnum() or js[j - 1] in "_$"):
            j -= 1
        return js[j:i] not in _KEYWORDS_BEFORE_TEMPLATE
    return False


def stage_literal_runs(js: str, name: str, stager: Stager) -> str:
    """``js`` with each large static run replaced by its pointer literal.

    A run is single-quoted literals joined by ` + `, or the static text of a template literal between its
    `${}` expressions (spliced back as `${pointer}`); double-quoted literals (JSON schemas) are never
    staged. The first run is staged as ``name``, later ones as ``name-2``, ``name-3`` in script order.
    """
    literals, dead = _scan(js)
    edits: list[tuple[int, int, Callable[[str], str]]] = []
    for run in _literal_runs(js, literals):
        texts = [_decode(js[s + 1 : e - 1]) for s, e in run]
        if any(t is None for t in texts):
            continue
        text = "".join(texts)  # type: ignore[arg-type]
        lead, tail = _code_span_edges(js, run, text)
        if lead == 0 and js[max(0, run[0][0] - len(_JOIN)) : run[0][0]] == _JOIN:
            lead = len(text[:tail]) - len(text[:tail].lstrip())
        body = text[lead:tail]
        if len(body.encode("utf-8")) < MIN_STAGE_BYTES and not needs_binding(body):
            continue

        def build(nm: str, text=text, lead=lead, tail=tail, body=body) -> str:
            parts = [_js_string_literal(text[:lead])] if lead else []
            parts.append(stager(nm, body))
            if tail < len(text):
                parts.append(_js_string_literal(text[tail:]))
            return _JOIN.join(parts)

        edits.append((run[0][0], run[-1][1], build))
    for ts, te in _top_level_templates(dead, js):
        segs = _template_segments(js, ts, te)
        for a, b in segs or ():
            text = _template_decode(js[a:b])
            if text is None:
                continue
            lead, tail = 0, len(text)
            if text.count("`") % 2:
                if a > ts + 1:
                    eol = text.find("\n", text.index("`"))
                    lead = len(text) if eol < 0 else eol + 1
                if b < te - 1:
                    tail = text.rfind("\n", 0, text.rindex("`")) + 1
                tail = max(lead, tail)
            elif b < te - 1 and not text.endswith("\n") and "\n" in text:
                tail = text.rfind("\n") + 1
            if lead == 0 and a > ts + 1:
                lead = len(text[:tail]) - len(text[:tail].lstrip())
            body = text[lead:tail]
            if len(body.encode("utf-8")) < MIN_STAGE_BYTES:
                continue

            def build_t(nm: str, text=text, lead=lead, tail=tail, body=body) -> str:
                return _template_encode(text[:lead]) + "${" + stager(nm, body) + "}" + _template_encode(text[tail:])

            edits.append((a, b, build_t))
    edits.sort(key=lambda t: t[0])
    out: list[str] = []
    last = 0
    for k, (s, e, make) in enumerate(edits, 1):
        out.append(js[last:s])
        out.append(make(name if k == 1 else f"{name}-{k}"))
        last = e
    out.append(js[last:])
    return "".join(out)


# Public names for cross-module callers (ask_compose, the bytes probe).
decode_literal = _decode
literal_runs = _literal_runs
