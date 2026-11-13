"""Pins both roadmap-fanout composed scripts (standalone, in-run M+ re-fire) under 90% of the Workflow runner byte cap.

Composed exactly as test_ask_roadmap_fanout_e2e._scripts does, cap in force. A refused compose carries
no script, so its bytes come from the refusal text; a composed script is measured as its UTF-8 length.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.compose_size_probe import CEILING_RATIO, compose_bytes, parse_cap_refusal
from coordinator_core.ops.dispatch_emit.tests.test_ask_roadmap_fanout_e2e import (
    _RB_TEXT,
    _compose_in_run,
    _compose_standalone,
    _put,
    repo,  # noqa: F401
)

pytestmark = pytest.mark.complete

_CEILING = int(CEILING_RATIO * emit._WORKFLOW_SCRIPT_BYTE_CAP)


def _measure(repo, which: str) -> dict:
    _put(repo)
    if which == "standalone":
        return compose_bytes(lambda: _compose_standalone(repo))
    return compose_bytes(lambda: _compose_in_run(roadmap_blitz_text=_RB_TEXT, in_run_commit=True))


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_roadmap_fanout_script_keeps_ten_percent_headroom(repo, which):
    got = _measure(repo, which)
    where = f" (largest inlined component {got['largest_component']['name']}: {got['largest_component']['bytes']} bytes)" if "largest_component" in got else ""
    state = "refused at compose" if got["refused"] else "composed"
    assert got["total"] <= _CEILING, f"{which} {state} at {got['total']} bytes > {_CEILING} ceiling{where}"


def test_parse_cap_refusal_reads_total_and_largest_component():
    text = "composed ask script is over the Workflow runner's 524288-byte cap: 533824 bytes; largest inlined component other body lines (120579 bytes)"
    assert parse_cap_refusal(text) == {
        "total": 533824, "refused": True, "largest_component": {"name": "other body lines", "bytes": 120579},
    }
    assert "largest_component" not in parse_cap_refusal("x runner's 524288-byte cap: 600000 bytes")
    with pytest.raises(ValueError):
        parse_cap_refusal("some other refusal")
