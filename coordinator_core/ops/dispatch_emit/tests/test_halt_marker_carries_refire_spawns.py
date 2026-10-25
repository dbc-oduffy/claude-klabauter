"""The marker a halt lands in git carries the refire envelope and the kept-work stage pointer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire
from coordinator_core.ops.dispatch_emit.tests.test_halt_marker_carries_refire import (
    HALTED_REL,
    KEPT_POINTER,
    _envelope,
    _halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_halt_records_kept_work import (  # noqa: F401
    WRITERS,
    _chunks,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _landed_marker(root: Path) -> dict:
    return json.loads(_git(root, "show", f"HEAD:{HALTED_REL}"))


@WRITERS
def test_landed_marker_carries_envelope_and_kept_pointer(repo, call):
    _chunks(repo, {"C1": ["a.py"]}, {"C1": "DONE: C1\n"})

    out = call(repo, _halted(_envelope()))

    assert out["committed"] is True, out
    action = _landed_marker(repo)["next_action"]
    assert halt_refire.is_refire_next_action(action)
    assert action["resume"]["stage"] == {"kept": KEPT_POINTER, "chunks": ["C1"], "plan_gate": False}


@WRITERS
def test_nothing_kept_leaves_stage_null(repo, call):
    out = call(repo, _halted(_envelope()))

    assert out["committed"] is True, out
    assert _landed_marker(repo)["next_action"]["resume"]["stage"] is None


@WRITERS
def test_legacy_string_next_action_is_omitted_from_landed_marker(repo, call):
    _chunks(repo, {"C1": ["a.py"]}, {"C1": "DONE: C1\n"})

    out = call(repo, _halted("fix the file"))

    assert out["committed"] is True, out
    assert "next_action" not in _landed_marker(repo)
