"""coordinator_core/ops/workflow_fire/tests/test_fired_drift_guard.py

The `workflow.fire` op's `expected` wire param: coordinator-content-repo parity port, leg 3
of `2026-09-21-bug-blitz-emitter-engine-leg.md`'s follow-on port
(`emit-dispatch-workflow.py :: _guard_against_fired_drift`, called at
`fire()` before `engine_fire.fire_workflow`). Ported into the engine's own
fire op (`coordinator_core.ops.workflow_fire.op :: _workflow_fire`) rather
than left to a caller's own pre-call discipline, so every caller of
`workflow.fire` gets the refusal, not only DoE's wrapper.

No spawn happens in the refusal case below -- `guard_against_fired_drift`
raises before `fire.fire_workflow` is ever called, so these tests need no
git fixture and spawn nothing.
"""

from __future__ import annotations

import hashlib

import pytest

from coordinator_core.ops.dispatch_emit.op import FiredDriftError
from coordinator_core.ops.workflow_fire.op import _workflow_fire


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_fire_refuses_a_script_edited_after_emit(tmp_path):
    """A peer (or the operator) overwrote the deterministic script path
    after this caller's emit returned but before it fired -- firing must
    refuse rather than run the bytes now on disk under this caller's
    handle. ``expected`` is the sha256 the caller emitted, never the
    script text (`dispatch.emit`'s reply `"sha256"` key)."""
    script_path = tmp_path / "plan.workflow.mjs"
    script_path.write_text("phase('Wave 1: C1');\n", encoding="utf-8")

    with pytest.raises(FiredDriftError):
        _workflow_fire(
            {
                "script_path": str(script_path),
                "expected": _sha256("phase('Wave 1: C1, C2');\n"),
            }
        )


def test_fire_with_expected_omitted_is_unverified_same_as_before(tmp_path, monkeypatch):
    """No ``expected`` param -- the guard never runs, and the call reaches
    ``fire.fire_workflow`` exactly as it did before this param existed."""
    script_path = tmp_path / "plan.workflow.mjs"
    script_path.write_text("phase('Wave 1: C1');\n", encoding="utf-8")

    called = {}

    def _fake_fire_workflow(script_path_arg, **kwargs):
        called["script_path"] = script_path_arg
        return {"fire_id": "fake"}

    import coordinator_core.ops.workflow_fire.op as op_mod

    monkeypatch.setattr(op_mod.fire, "fire_workflow", _fake_fire_workflow)

    result = _workflow_fire({"script_path": str(script_path)})

    assert result == {"fire_id": "fake"}
    assert called["script_path"] == str(script_path)


def test_fire_with_expected_matching_on_disk_bytes_proceeds(tmp_path, monkeypatch):
    """``expected`` equal to the on-disk bytes is the ordinary case: the
    guard is silent and the call still reaches ``fire.fire_workflow``."""
    script_path = tmp_path / "plan.workflow.mjs"
    script_text = "phase('Wave 1: C1');\n"
    script_path.write_text(script_text, encoding="utf-8", newline="\n")

    called = {}

    def _fake_fire_workflow(script_path_arg, **kwargs):
        called["script_path"] = script_path_arg
        return {"fire_id": "fake"}

    import coordinator_core.ops.workflow_fire.op as op_mod

    monkeypatch.setattr(op_mod.fire, "fire_workflow", _fake_fire_workflow)

    result = _workflow_fire(
        {"script_path": str(script_path), "expected": _sha256(script_text)}
    )

    assert result == {"fire_id": "fake"}
    assert called["script_path"] == str(script_path)
