"""P071-C5: bug-sweep emits the two `check` rechecks its own doctrine
mandates (`bug-sweep/SKILL.md:91,163`) via `readers_sweep.collect`.

Pins the two properties this row asserts for bug-sweep (mirroring
`test_readers_blitz.py`'s pins for bug-blitz's single recheck): both check
directives carry a `depends_on` edge to the grant's own write directive
(never its judgment-point id), and neither introduces a second judgment
point.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.backlog_grind_assemble import readers_sweep


def _collect_bug_sweep(tmp_path: Path, monkeypatch) -> object:
    monkeypatch.setattr(readers_sweep, "_resolve_state_root", lambda: str(tmp_path / "state"))
    monkeypatch.setattr(readers_sweep, "load_family_records", lambda *a, **k: [])
    return readers_sweep.collect("bug-sweep")


def _grant_write_directive(result) -> dict:
    for directive in result.directives:
        if directive["id"] == "d-bug-sweep-tier-u-grant-write":
            return directive
    raise AssertionError("grant write directive not found")


def _grant_check_directive(result, directive_id: str) -> dict:
    for directive in result.directives:
        if directive["id"] == directive_id:
            return directive
    raise AssertionError(f"{directive_id} not found")


def test_grant_flow_present_for_bug_sweep_cadence(tmp_path: Path, monkeypatch) -> None:
    result = _collect_bug_sweep(tmp_path, monkeypatch)
    write_directive = _grant_write_directive(result)
    pre_check = _grant_check_directive(
        result, "d-bug-sweep-tier-u-grant-check-pre-track-b"
    )
    post_check = _grant_check_directive(
        result, "d-bug-sweep-tier-u-grant-check-post-fix"
    )
    assert write_directive["cli"] == "tier-u-grant-cli"
    assert write_directive["args"][0] == "grant"
    assert pre_check["cli"] == "tier-u-grant-cli"
    assert pre_check["args"] == ["check"]
    assert post_check["cli"] == "tier-u-grant-cli"
    assert post_check["args"] == ["check"]

    jp_ids = {jp["id"] for jp in result.judgment_points}
    assert "j-bug-sweep-tier-u-grant" in jp_ids


def test_grant_flow_absent_for_every_other_cadence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        readers_sweep, "_resolve_state_root", lambda: str(tmp_path / "state")
    )
    monkeypatch.setattr(readers_sweep, "load_family_records", lambda *a, **k: [])
    for cadence in ("bug-blitz", "mise-en-place", "debt-triage", "dogfood"):
        result = readers_sweep.collect(cadence)
        assert result.directives == []
        assert result.judgment_points == []


def test_both_rechecks_depend_on_the_grant_write_directive(
    tmp_path: Path, monkeypatch
) -> None:
    result = _collect_bug_sweep(tmp_path, monkeypatch)
    write_directive = _grant_write_directive(result)
    pre_check = _grant_check_directive(
        result, "d-bug-sweep-tier-u-grant-check-pre-track-b"
    )
    post_check = _grant_check_directive(
        result, "d-bug-sweep-tier-u-grant-check-post-fix"
    )
    assert pre_check["depends_on"] == write_directive["id"]
    assert post_check["depends_on"] == write_directive["id"]


def test_rechecks_carry_no_second_judgment_point(tmp_path: Path, monkeypatch) -> None:
    result = _collect_bug_sweep(tmp_path, monkeypatch)
    # Exactly one judgment point (the grant's) is emitted by the whole
    # grant flow -- neither check directive introduces a second one.
    grant_jp_ids = [
        jp["id"] for jp in result.judgment_points if jp["id"] == "j-bug-sweep-tier-u-grant"
    ]
    assert len(grant_jp_ids) == 1
    assert all(
        jp["id"]
        not in (
            "d-bug-sweep-tier-u-grant-check-pre-track-b",
            "d-bug-sweep-tier-u-grant-check-post-fix",
        )
        for jp in result.judgment_points
    )


def test_rechecks_carry_no_already_satisfied(tmp_path: Path, monkeypatch) -> None:
    result = _collect_bug_sweep(tmp_path, monkeypatch)
    pre_check = _grant_check_directive(
        result, "d-bug-sweep-tier-u-grant-check-pre-track-b"
    )
    post_check = _grant_check_directive(
        result, "d-bug-sweep-tier-u-grant-check-post-fix"
    )
    assert "already_satisfied" not in pre_check
    assert "already_satisfied" not in post_check


def test_two_rechecks_are_two_distinct_directives(tmp_path: Path, monkeypatch) -> None:
    # SKILL.md:91's pre-Track-B recheck and :163's post-fix recheck are two
    # independently-dispatchable directives, not one directive reported
    # twice -- a revoked/dead-session grant between the two points is what
    # each independently re-checks liveness against.
    result = _collect_bug_sweep(tmp_path, monkeypatch)
    check_ids = {
        d["id"] for d in result.directives if d["cli"] == "tier-u-grant-cli" and d["args"] == ["check"]
    }
    assert check_ids == {
        "d-bug-sweep-tier-u-grant-check-pre-track-b",
        "d-bug-sweep-tier-u-grant-check-post-fix",
    }
