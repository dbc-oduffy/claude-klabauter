from __future__ import annotations

from pathlib import Path

from coordinator_core.backlog_grind_assemble import readers_blitz


def _collect_bug_blitz(tmp_path: Path, monkeypatch) -> object:
    monkeypatch.setattr(readers_blitz, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(readers_blitz, "load_family_records", lambda *a, **k: [])
    return readers_blitz.collect("bug-blitz")


def _grant_write_directive(result) -> dict:
    for directive in result.directives:
        if directive["id"] == "d-bug-blitz-tier-u-grant-write":
            return directive
    raise AssertionError("grant write directive not found")


def _grant_check_directive(result) -> dict:
    for directive in result.directives:
        if directive["id"] == "d-bug-blitz-tier-u-grant-check":
            return directive
    raise AssertionError("grant check directive not found")


def test_grant_flow_present_for_bug_blitz_cadence(tmp_path: Path, monkeypatch) -> None:
    result = _collect_bug_blitz(tmp_path, monkeypatch)
    write_directive = _grant_write_directive(result)
    check_directive = _grant_check_directive(result)
    assert write_directive["cli"] == "tier-u-grant-cli"
    assert write_directive["args"][0] == "grant"
    assert check_directive["cli"] == "tier-u-grant-cli"
    assert check_directive["args"] == ["check"]

    jp_ids = {jp["id"] for jp in result.judgment_points}
    assert "j-bug-blitz-tier-u-grant" in jp_ids


def test_grant_flow_absent_for_every_other_cadence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(readers_blitz, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(readers_blitz, "load_family_records", lambda *a, **k: [])
    for cadence in ("mise-en-place", "bug-sweep", "debt-triage", "dogfood"):
        result = readers_blitz.collect(cadence)
        assert result.directives == []
        assert result.judgment_points == []


def test_confirm_green_check_depends_on_the_grant_write_directive(
    tmp_path: Path, monkeypatch
) -> None:
    result = _collect_bug_blitz(tmp_path, monkeypatch)
    write_directive = _grant_write_directive(result)
    check_directive = _grant_check_directive(result)
    assert check_directive["depends_on"] == write_directive["id"]


def test_confirm_green_check_carries_no_second_judgment_point(
    tmp_path: Path, monkeypatch
) -> None:
    result = _collect_bug_blitz(tmp_path, monkeypatch)
    grant_jp_ids = [
        jp["id"] for jp in result.judgment_points if jp["id"] == "j-bug-blitz-tier-u-grant"
    ]
    assert len(grant_jp_ids) == 1
    assert all(
        jp["id"] != "d-bug-blitz-tier-u-grant-check" for jp in result.judgment_points
    )


def test_confirm_green_check_carries_no_already_satisfied(
    tmp_path: Path, monkeypatch
) -> None:
    result = _collect_bug_blitz(tmp_path, monkeypatch)
    check_directive = _grant_check_directive(result)
    assert "already_satisfied" not in check_directive
