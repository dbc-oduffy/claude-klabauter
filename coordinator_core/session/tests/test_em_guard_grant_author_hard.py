"""em_guard_grant refuses the bump pair while out_of_repo_write_block is on."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.session import em_guard_grant as eg

_FLAG_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK"


def _set_flag(monkeypatch, value: str) -> None:
    monkeypatch.setenv(_FLAG_ENV, value)
    machine_profile.reset_cache()


def _seam(monkeypatch, tmp_path):
    sdir = tmp_path / "sessions" / "s1"
    sentinel = tmp_path / "sentinels" / "s1-unlock"
    monkeypatch.setattr(eg.core, "ensure_session", lambda sid, cwd=None, **k: (sdir.mkdir(parents=True, exist_ok=True), str(sdir))[1])
    monkeypatch.setattr(eg.core, "resolve_session_id", lambda cwd=None: "s1")
    monkeypatch.setattr(eg.guard_unlock_sentinel, "sentinel_path", lambda sid, name: sentinel)
    return sdir, sentinel


@pytest.mark.parametrize("guard", sorted(eg._GRANTABLE_GUARDS))
def test_grant_refused_under_flag_and_nothing_written(monkeypatch, tmp_path, guard):
    _set_flag(monkeypatch, "on")
    sdir, sentinel = _seam(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not grantable on this box"):
        eg.write_em_guard_grant(guard, "because")
    assert not sdir.exists()
    assert not sentinel.exists()


def test_cli_grant_exits_2_under_flag(monkeypatch, tmp_path, capsys):
    _set_flag(monkeypatch, "on")
    _seam(monkeypatch, tmp_path)
    assert eg.main(["grant", "bump-foreign-repo-write", "because"]) == 2
    err = capsys.readouterr().err
    assert "bump-foreign-repo-write is not grantable on this box" in err
    assert "coordinator.feature.out_of_repo_write_block" in err


@pytest.mark.parametrize("guard", sorted(eg._GRANTABLE_GUARDS))
def test_grant_succeeds_with_flag_off(monkeypatch, tmp_path, guard):
    _set_flag(monkeypatch, "off")
    sdir, sentinel = _seam(monkeypatch, tmp_path)
    assert eg.write_em_guard_grant(guard, "because") is True
    assert (sdir / "em-guard-grant.json").is_file()
    assert sentinel.exists()


def test_grantable_set_unchanged():
    assert eg._GRANTABLE_GUARDS == frozenset(
        {"bump-foreign-repo-write", "bump-outside-repo-write"}
    )
