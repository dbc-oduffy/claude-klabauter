"""The failing-test blocker: one failing node id plus its message, as a halt's gap line.

Pure: zero spawns, no I/O. The JS fragment in `ask_compose` mirrors `from_reverify`,
`own_diff_blockers` and `gap_line` (the halt is built in the emitted script); the constants are
what Python imports. `TestBlocker`/`from_reverify`/`own_diff_blockers` are the reference the JS
must match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping

TEST_BLOCKER = "failing_test"
FAILING_DETAIL_FIELD = "failing_detail"
OWN_DIFF_PATHS_FIELD = "review_declared_paths"
_MAX_MESSAGE = 300


@dataclass(frozen=True)
class TestBlocker:
    __test__ = False

    node_id: str
    message: str = ""

    def gap_line(self) -> str:
        """`<node_id>: <first line of message, at most 300 chars>`."""
        lines = self.message.strip().splitlines()
        return f"{self.node_id}: {lines[0].strip()[:_MAX_MESSAGE] if lines else ''}"


def _detail(result: Mapping[str, Any]) -> dict:
    """`failing_detail` (a list of {node_id, message}) as {node_id: message}."""
    raw = result.get(FAILING_DETAIL_FIELD)
    out: dict = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping) and item.get("node_id"):
                msg = item.get("message")
                out[str(item["node_id"])] = "" if msg is None else str(msg)
    return out


def from_reverify(result: Mapping[str, Any]) -> List[TestBlocker]:
    """Blockers for `failing` minus `pre_existing`, `sibling_owned`, `foreign_red` (a {node: paths}
    map; its keys) and `kept`, sorted by node id.

    Messages come from `failing_detail`; a node without detail has an empty message.
    """
    ids = result.get("failing") or []
    if isinstance(ids, str):
        ids = [ids]
    excluded = (
        set(result.get("pre_existing") or [])
        | set(result.get("sibling_owned") or [])
        | set(result.get("foreign_red") or [])
        | set(result.get("kept") or [])
    )
    detail = _detail(result)
    return [
        TestBlocker(n, detail.get(n, ""))
        for n in sorted({str(i) for i in ids} - excluded)
    ]


def _norm_path(p: str) -> str:
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def own_diff_blockers(result: Mapping[str, Any], own_paths) -> List[TestBlocker]:
    """`from_reverify` blockers whose node id's file part is in `own_paths`.

    The file part is the text before the first `::`; both sides are normalised to `/`
    separators with a leading `./` stripped.
    """
    own = {_norm_path(p) for p in own_paths or []}
    return [b for b in from_reverify(result) if _norm_path(b.node_id.split("::", 1)[0]) in own]
