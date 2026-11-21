"""Per-call cost pins for the Write/Edit guards that run on every hook: the repo-identity
registry scan and the carve-out TOML parse must not repeat per call."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.write_guards import guard_concrete_path_citations as cites
from coordinator_core.write_guards import guard_doctrine_surface_edits as doctrine


def _payload(target: Path, sid: str) -> dict:
    return {
        "tool_name": "Write",
        "session_id": sid,
        "tool_input": {"file_path": str(target), "content": "x\n"},
    }


def _arm_doctrine(monkeypatch, tmp_path: Path) -> list:
    calls: list = []
    monkeypatch.setattr(doctrine, "_git_root", lambda: str(tmp_path))
    monkeypatch.setattr(doctrine, "resolve_git_common_dir", lambda _r: tmp_path / ".git")
    monkeypatch.setattr(
        doctrine,
        "compute_repo_identity_gate",
        lambda *_a, **_k: calls.append(1) or {"verdict": "MATCH", "message": "m"},
    )
    doctrine._identity_logged_at.clear()
    return calls


def test_no_session_dir_never_runs_the_registry_scan(monkeypatch, tmp_path):
    calls = _arm_doctrine(monkeypatch, tmp_path)
    for _ in range(5):
        doctrine.check(_payload(tmp_path / "a.txt", "sid-none"))
    assert calls == []


def test_identity_scan_runs_once_per_interval_with_a_session_dir(monkeypatch, tmp_path):
    calls = _arm_doctrine(monkeypatch, tmp_path)
    session_dir_for(tmp_path / ".git", "sid-live").mkdir(parents=True)
    for _ in range(5):
        doctrine.check(_payload(tmp_path / "a.txt", "sid-live"))
    assert len(calls) == 1
    assert (session_dir_for(tmp_path / ".git", "sid-live") / "repo-identity-gate.log").is_file()


def test_carve_outs_parsed_once_until_the_file_changes(monkeypatch, tmp_path):
    from coordinator_core.ops.session.abs_path_carve_outs import CARVE_OUTS_FILENAME

    parses: list = []
    real = cites.load_carve_outs
    monkeypatch.setattr(cites, "load_carve_outs", lambda r: parses.append(1) or real(r))
    cites._CARVE_OUTS_CACHE.clear()
    (tmp_path / CARVE_OUTS_FILENAME).write_text("[registry_keys]\nkeys = ['a.b']\n", encoding="utf-8")
    first = cites._carve_outs_cached(tmp_path)
    for _ in range(4):
        assert cites._carve_outs_cached(tmp_path) is first
    assert len(parses) == 1
    (tmp_path / CARVE_OUTS_FILENAME).write_text("[registry_keys]\nkeys = ['a.b', 'c.d']\n", encoding="utf-8")
    assert cites._carve_outs_cached(tmp_path).keys == frozenset({"a.b", "c.d"})
    assert len(parses) == 2
