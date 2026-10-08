"""Cloud-basis cells for the bug-sweep / bug-blitz Tier-U grant flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import readers_blitz, readers_sweep


def _sweep(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(readers_sweep, "_resolve_state_root", lambda: str(tmp_path / "state"))
    monkeypatch.setattr(readers_sweep, "load_family_records", lambda *a, **k: [])
    return readers_sweep.collect("bug-sweep")


def _blitz(monkeypatch):
    monkeypatch.setattr(readers_blitz, "_repo_root", lambda: None)
    return readers_blitz._tier_u_grant_flow()


@pytest.mark.parametrize("cloud", [True, False])
def test_sweep_grant_flow(tmp_path, monkeypatch, cloud):
    monkeypatch.setattr(readers_sweep, "cloud_box_basis", lambda *a, **k: "cloud-box" if cloud else None)
    result = _sweep(tmp_path, monkeypatch)
    jp_ids = [j["id"] for j in result.judgment_points]
    ids = {d["id"]: d for d in result.directives}
    assert ("j-bug-sweep-tier-u-grant" in jp_ids) is (not cloud)
    assert ("d-bug-sweep-tier-u-grant-write" in ids) is (not cloud)
    for k in ("pre-track-b", "post-fix"):
        d = ids[f"d-bug-sweep-tier-u-grant-check-{k}"]
        assert d["args"] == ["check"]
        assert d["depends_on"] == (None if cloud else "d-bug-sweep-tier-u-grant-write")


@pytest.mark.parametrize("cloud", [True, False])
def test_blitz_grant_flow(monkeypatch, cloud):
    monkeypatch.setattr(readers_blitz, "cloud_box_basis", lambda *a, **k: "cloud-box" if cloud else None)
    result = _blitz(monkeypatch)
    assert ("j-bug-blitz-tier-u-grant" in [j["id"] for j in result.judgment_points]) is (not cloud)
    ids = {d["id"]: d for d in result.directives}
    assert ("d-bug-blitz-tier-u-grant-write" in ids) is (not cloud)
    assert ids["d-bug-blitz-tier-u-grant-check"]["depends_on"] == (
        None if cloud else "d-bug-blitz-tier-u-grant-write"
    )
