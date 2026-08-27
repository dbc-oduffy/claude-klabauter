"""
coordinator_core.workstream_complete.test_lesson_capture_reachable — AC15
evidence (docs/plans/2026-08-25-the-close-ceremony-rebuilt-from-the-
requirement.md, chunk C11, DR-358 § Negative-spec ruling).

Purpose: proves the lesson-capture step (`coordinator-lesson-add` /
`coordinator-queue-append`) stops being an UNCONDITIONAL mandate in
`build_directives`'s emitted `directives[]` and gets a ceremony-owned,
documented hand-write fallback (`preflight.lesson_capture_route`) instead,
whenever `_is_dispatch_engine_stamped()` is False -- the exact state this
clone is in (no `coordinator_core/_engine_stamp`), and the exact scenario
that made two prior sessions hand-write lesson YAML against a sibling
record's field set.

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_lesson_capture_reachable.py -q

Negative-spec:
    - Does NOT exercise the real `coordinator-lesson-add`/`coordinator-
      queue-append` CLIs (never invoked in-process, per this module's own
      Negative-spec) -- only the compute-only `brief()`/`build_directives`
      seam that decides whether either name reaches `directives[]`.
    - Does NOT assert anything about `CONSUMES_MANIFEST` membership --
      both names stay members regardless of reachability (see that
      constant's own docstring for why: `apply.py::_CLI_DISPATCH` is built
      from the same tuple).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coordinator_core.workstream_complete as wsc

pytestmark = [pytest.mark.cadence]


def _gate(disposition: str = "single-session") -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid="lesson-reachable-sid",
        disposition=disposition,
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )


def _lesson_decisions() -> dict[str, Any]:
    return {
        "lessons": [
            {
                "title": "AC15 fixture lesson",
                "body": "AC15 fixture lesson body",
                "scope": "project",
            }
        ],
    }


def _patch_reachable(monkeypatch: pytest.MonkeyPatch, reachable: bool) -> None:
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: _gate())
    monkeypatch.setattr(wsc, "_lesson_capture_reachable", lambda: reachable)


@pytest.mark.spawns_process
def test_lesson_capture_directive_fires_when_producer_reachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reachable (`_engine_stamp` present, or a stamped host): the pre-C11
    behaviour is unchanged -- the lesson supplied via `decisions["lessons"]`
    still reaches `directives[]` naming the real CLI."""
    _patch_reachable(monkeypatch, True)
    decision_object = wsc.brief(decisions=_lesson_decisions(), repo_root=tmp_path)
    clis = {d["cli"] for d in decision_object["directives"]}
    assert "coordinator-lesson-add" in clis

    route = decision_object["preflight"]["lesson_capture_route"]
    assert route["reachable"] is True
    assert route["fallback"] is None


@pytest.mark.spawns_process
def test_lesson_capture_directive_absent_when_producer_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unreachable (this clone's actual state, no `_engine_stamp`): AC15's
    fix. `coordinator-lesson-add`/`coordinator-queue-append` no longer
    reach `directives[]` even though `decisions["lessons"]` is populated --
    the mandate this chunk removes -- and the ceremony's OWN documented
    fallback surfaces via `preflight.lesson_capture_route` in its place,
    carrying the real vendored `lesson-entry` schema (never a sibling
    record's field set)."""
    _patch_reachable(monkeypatch, False)
    decision_object = wsc.brief(decisions=_lesson_decisions(), repo_root=tmp_path)
    clis = {d["cli"] for d in decision_object["directives"]}
    assert "coordinator-lesson-add" not in clis
    assert "coordinator-queue-append" not in clis

    route = decision_object["preflight"]["lesson_capture_route"]
    assert route["reachable"] is False
    fallback = route["fallback"]
    assert fallback is not None
    schema = fallback["schema"]
    assert schema["applies_to"] == "state/lessons/*.yaml"
    assert "title" in schema["required"]
    assert "body" in schema["required"]
    assert "scope" in schema["required"]


@pytest.mark.spawns_process
def test_lesson_worth_capturing_resolves_no_phantom_id_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `lesson-worth-capturing` judgment point's `resolves` list must
    agree with what `directives[]` actually built -- naming `d-add-lesson-1`
    while unreachable would be a phantom `resolves` entry
    (`ceremony_common.test_phantom_resolves_id_sweep`'s own concern), since
    no directive by that id was ever emitted this pass."""
    _patch_reachable(monkeypatch, False)
    decision_object = wsc.brief(decisions=_lesson_decisions(), repo_root=tmp_path)
    lesson_jp = next(
        jp for jp in decision_object["judgment_points"] if jp["id"] == "lesson-worth-capturing"
    )
    capture_disposition = next(
        d for d in lesson_jp["dispositions"] if d.get("value") == "capture"
    )
    assert "d-add-lesson-1" not in capture_disposition.get("resolves", [])
