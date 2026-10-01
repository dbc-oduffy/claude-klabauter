"""close_dispatched / land_wave refuse a `shipped_in` that is not a commit reachable from HEAD."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap import plan_gate as pg
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

NO_COMMIT = "substantively-shipped-no-commit:2026-10-01"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-q", "-m", "landed")
    landed = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "-b", "fire")
    (tmp_path / "f").write_text("2")
    _git(tmp_path, "commit", "-q", "-am", "pre-landing")
    stray = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "main")
    return tmp_path, landed, stray


def _baton(root: Path, stub_id: str) -> str:
    p = root / "state" / "handoffs" / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\nkind: roadmap-baton\ntitle: t\nstub_id: %s\nstatus: open\n"
        "deployment_state: ready_to_fire\nbaton_role: work\n---\n\nbody\n" % stub_id,
        encoding="utf-8",
    )
    return f"state/handoffs/{stub_id}.md"


def test_a_reachable_sha_is_accepted(repo):
    root, landed, _ = repo
    baton = _baton(root, "b-1")
    out = bl.close_dispatched(root, baton, landed[:10])
    assert out["closed"] is True
    assert pg._read_baton_fields(root / baton)["shipped_in"] == landed[:10]


def test_a_resolving_sha_that_is_not_an_ancestor_is_refused(repo):
    root, _, stray = repo
    baton = _baton(root, "b-1")
    with pytest.raises(bl.LandingRefused) as exc:
        bl.close_dispatched(root, baton, stray)
    msg = str(exc.value)
    assert stray in msg and "main" in msg and "substantively-shipped-no-commit:" in msg
    assert "shipped_in" not in (root / baton).read_text(encoding="utf-8")


def test_an_unknown_sha_is_refused(repo):
    root, landed, _ = repo
    baton = _baton(root, "b-1")
    with pytest.raises(bl.LandingRefused) as exc:
        bl.close_dispatched(root, baton, "deadbee")
    assert "deadbee" in str(exc.value)


def test_the_no_commit_form_is_accepted_without_a_git_spawn(repo, monkeypatch):
    root, _, _ = repo
    baton = _baton(root, "b-1")
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: pytest.fail("git spawned"))
    out = bl.close_dispatched(root, baton, NO_COMMIT)
    text = (root / baton).read_text(encoding="utf-8")
    assert out["closed"] is True
    assert f"shipped_in: {NO_COMMIT}" in text and "shipped_in_kind: no-commit" in text


def test_a_wave_of_n_batons_costs_one_git_spawn(repo, monkeypatch):
    root, landed, _ = repo
    ids = [f"b-{i}" for i in range(4)]
    for i in ids:
        _baton(root, i)
    calls = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda args, **k: calls.append(args) or real(args, **k))
    out = bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": i, "route": "dispatch"} for i in ids]},
        shipped_in=landed,
    )
    assert [r["closed"] for r in out["closed"]] == [True] * 4 and not out["refused"]
    assert len(calls) == 1


def test_a_wave_citing_the_stray_sha_refuses_every_baton(repo):
    root, _, stray = repo
    _baton(root, "b-1")
    out = bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "b-1", "route": "dispatch"}]},
        shipped_in=stray,
    )
    assert not out["closed"] and stray in out["refused"][0]["reason"]
