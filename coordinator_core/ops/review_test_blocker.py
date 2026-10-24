"""The failing-test blocker: one failing node id plus its message, as a halt's gap line.

Pure: zero spawns, no I/O. `ask_compose._review_blocked_halt_js` re-implements `from_reverify`
and `gap_line` in JS (the halt is built in the emitted script); the constants are what Python
imports. `TestBlocker`/`from_reverify` are the reference the JS must match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping

TEST_BLOCKER = "failing_test"
FAILING_DETAIL_FIELD = "failing_detail"
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
    """Blockers for `failing` minus `pre_existing` and `kept`, sorted by node id.

    Messages come from `failing_detail`; a node without detail has an empty message.
    """
    ids = result.get("failing") or []
    if isinstance(ids, str):
        ids = [ids]
    excluded = set(result.get("pre_existing") or []) | set(result.get("kept") or [])
    detail = _detail(result)
    return [
        TestBlocker(n, detail.get(n, ""))
        for n in sorted({str(i) for i in ids} - excluded)
    ]
