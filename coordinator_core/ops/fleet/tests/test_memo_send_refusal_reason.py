"""
Purpose: every memo.send refusal returns its reason in the `reason` envelope field,
which cc_invoke.route_mutation reads, so the reason survives the warm transport
(stderr / the warm diagnostic channel can be dropped).
"""

from __future__ import annotations

from coordinator_core.ops.fleet.memo_send import _memo_send
from pathlib import Path
import pytest


def test_param_and_setup_refusals_carry_reason(tmp_path: Path) -> None:
    result = _memo_send({"dry_run": True, "topic": "x"}, repo_root=None)
    assert result["exit_code"] == 1
    assert "no repo_root supplied" in result["reason"]

    bad = _memo_send({"dry_run": "yes", "topic": "x"}, repo_root=None)
    assert "dry_run must be bool" in bad["reason"]
