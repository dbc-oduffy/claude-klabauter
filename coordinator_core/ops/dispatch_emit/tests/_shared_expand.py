"""Read an emitted script back with every ``_shared`` reference inlined.

Tests assert on what an agent is actually handed. ``emit.SharedBlocks``
declares repeated prompt text once and references it; this inverts that, so an
assertion written against the inlined prompt holds either way.
"""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.workflow_scaffold import _js_string_literal

_DECL_RE = re.compile(r"  const _shared = \[\n(?P<items>.*?)\n  \];\n\n", re.S)
_ITEM_RE = re.compile(r"    `((?:[^`\\]|\\.)*)`", re.S)


def _unescape_template(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text, flags=re.S)


def expand_shared(script: str) -> str:
    match = _DECL_RE.search(script)
    if match is None:
        return script
    texts = [_unescape_template(m.group(1)) for m in _ITEM_RE.finditer(match.group("items"))]
    out = script[: match.start()] + script[match.end():]
    for index, text in enumerate(texts):
        out = out.replace(f"_shared[{index}] + '", "'" + _js_string_literal(text)[1:-1])
        out = out.replace("${_shared[%d]}" % index, emit._escape_for_js_template_literal(text))
    return out
