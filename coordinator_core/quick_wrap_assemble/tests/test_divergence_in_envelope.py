"""`brief()` carries diverged run-report prose as the `j-diverged-sidecars` judgment point.

Facts and session resolution are stubbed, so no git or session state is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.quick_wrap_assemble as qwa

# The spawn is statically reachable from the code under test; tiered so a future change cannot spawn on the fast tier.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "abcdefab-cdef-abcd-efab-cdefabcdefab"
_PROSE = "Planned a rename; found the symbol is public API so I kept the alias."
_REL = f".coordinator-local/subagent-share/{_SID}/coordinator-executor.abc.md"


def _sidecar(diverged: bool) -> str:
    return (
        "---\n"
        "plan: p.md\n"
        "agent_type: coordinator-executor\n"
        "divergence:\n"
        f"  diverged: {'true' if diverged else 'false'}\n"
        "---\n\n"
        "## Divergence from plan\n\n"
        f"{_PROSE}\n"
    )


@pytest.fixture
def stubbed(tmp_path: Path, monkeypatch):
    def _degraded(*args, **kwargs):
        return {"degraded": True, "evidence": "test stub", "value": None}

    for name in (
        "session_pickup_kind",
        "session_governing_plan",
        "session_diff_brightline",
        "session_terminal_sizings",
        "session_fold_sidecars",
    ):
        monkeypatch.setattr(qwa.session_facts, name, _degraded)
    monkeypatch.setattr(
        qwa, "_resolve_session", lambda wr=None: (tmp_path, tmp_path / ".git", _SID)
    )
    monkeypatch.setattr(qwa, "_print_commits_into_baton", lambda *a, **k: None)
    return tmp_path


def _write(root: Path, diverged: bool) -> None:
    path = root / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sidecar(diverged), encoding="utf-8")


def _points(envelope: dict, id_: str) -> list[dict]:
    return [p for p in envelope["judgment_points"] if p["id"] == id_]


def test_diverged_prose_and_path_reach_the_envelope(stubbed):
    _write(stubbed, True)
    envelope = qwa.brief(stubbed, commit=False)
    (point,) = _points(envelope, "j-diverged-sidecars")
    assert _PROSE in point["evidence"]
    assert _REL in point["evidence"]


def test_not_diverged_leaves_no_point_and_no_prose(stubbed):
    _write(stubbed, False)
    envelope = qwa.brief(stubbed, commit=False)
    assert _points(envelope, "j-diverged-sidecars") == []
    assert _PROSE not in json.dumps(envelope)


def test_degraded_scan_appends_degraded_point(stubbed, monkeypatch):
    monkeypatch.setattr(
        qwa,
        "collect_diverged_sidecars",
        lambda root, sid: {"degraded": True, "evidence": "boom: OSError"},
    )
    envelope = qwa.brief(stubbed, commit=False)
    (point,) = _points(envelope, "j-diverged-sidecars-degraded")
    assert "boom: OSError" in point["evidence"]
    assert _points(envelope, "j-diverged-sidecars") == []
