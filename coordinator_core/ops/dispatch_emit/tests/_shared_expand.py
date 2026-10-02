
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
    texts = [
        _unescape_template(m.group(1).replace("${_repoRoot}", emit._SHARED_PATH_MARKER_DELIM))
        for m in _ITEM_RE.finditer(match.group("items"))
    ]
    out = script[: match.start()] + script[match.end():]
    for index, text in enumerate(texts):
        plus = "' + _repoRoot + '".join(
            _js_string_literal(part)[1:-1] for part in text.split(emit._SHARED_PATH_MARKER_DELIM)
        )
        out = out.replace(f"_shared[{index}] + '", "'" + plus)
        template = "${_repoRoot}".join(
            emit._escape_for_js_template_literal(part)
            for part in text.split(emit._SHARED_PATH_MARKER_DELIM)
        )
        out = out.replace("${_shared[%d]}" % index, template)
    return out
