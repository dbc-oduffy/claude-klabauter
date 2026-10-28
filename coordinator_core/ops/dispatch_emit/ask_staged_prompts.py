"""Stage the large static runs of a composed ask prompt as record-dir files the agent reads by path.

`stage` records one body into the caller's sink and returns the JS literal that stands in for it;
`stage_literal_runs` finds the bodies inside a prompt expression. The composed script holds the
pointer, the engine writes the sink to `<record_dir>/prompts/<name>.md` (op.py), and the agent
receives the same text through Read. Composition stays pure: nothing here touches disk.

Trap: a run is staged only when every escape in it is one this module decodes; any other escape
leaves the run inline rather than guessing its text.
"""

from __future__ import annotations

import re
from typing import Callable

from coordinator_core.ops.dispatch_emit.prompt_fragments import _scan_literals
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

    ``record_dir`` is spelled as the stage spells the ask text path, repo-relative, so a leg run
    from another cwd resolves it the same way. A name already recorded with other text is refused.
    """
    if sink.get(name, text) != text:
        raise ValueError(f"staged prompt {name!r} already recorded with different text")
    sink[name] = text
    return _js_string_literal(
        f"Read {record_dir}/{PROMPTS_DIR}/{name}.md in full and follow it as the rest of this brief.\n"
    )


def _decode(content: str) -> str | None:
    """The text a single-quoted literal's escaped ``content`` denotes, or None on an escape not decoded."""
    for m in _ESC.finditer(content):
        if m.group(1) not in _ESCAPES:
            return None
    return _ESC.sub(lambda m: _ESCAPES[m.group(1)], content)


def stage_literal_runs(js: str, name: str, stager: Stager) -> str:
    """``js`` with each large run of adjacent single-quoted literals replaced by its pointer literal.

    A run is literals joined by ` + `; double-quoted literals (JSON schemas) are never staged. The
    first run is staged as ``name``, later ones as ``name-2``, ``name-3`` in script order.
    """
    spans = [(s, e) for s, e in _scan_literals(js) if js[s] == "'"]
    runs: list[list[tuple[int, int]]] = []
    for span in spans:
        if runs and js[runs[-1][-1][1] : span[0]] == _JOIN:
            runs[-1].append(span)
        else:
            runs.append([span])
    out: list[str] = []
    last = 0
    staged = 0
    for run in runs:
        texts = [_decode(js[s + 1 : e - 1]) for s, e in run]
        if any(t is None for t in texts):
            continue
        text = "".join(texts)  # type: ignore[arg-type]
        if len(text.encode("utf-8")) < MIN_STAGE_BYTES:
            continue
        staged += 1
        out.append(js[last : run[0][0]])
        out.append(stager(name if staged == 1 else f"{name}-{staged}", text))
        last = run[-1][1]
    out.append(js[last:])
    return "".join(out)
