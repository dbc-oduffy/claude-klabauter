"""provenance_gaps: read_set mirrors pages_changed, gaps are commit dates minus daily files, one git spawn."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.architecture_survey import provenance_gaps as pg


def _commit(repo: Path, name: str, day: str) -> None:
    (repo / name).write_text("x\n")
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{day}T12:00:00", "GIT_COMMITTER_DATE": f"{day}T12:00:00"}
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name],
                   check=True, env=env)


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _commit(tmp_path, "a", "2026-11-10")
    _commit(tmp_path, "b", "2026-11-10")
    _commit(tmp_path, "c", "2026-11-11")
    _commit(tmp_path, "d", "2026-11-12")
    return tmp_path


def test_gaps_are_commit_dates_without_daily_entry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    daily = repo / "archive" / "daily-summaries"
    daily.mkdir(parents=True)
    (daily / "2026-11-11.md").write_text("x\n")
    (daily / "2026-11-01.md").write_text("x\n")
    gaps = pg.provenance_gaps(repo, "--since=2026-01-01")
    assert [g["date"] for g in gaps] == ["2026-11-10", "2026-11-12"]
    assert gaps[0]["missing"] == "archive/daily-summaries/2026-11-10.md"


def test_suffixed_daily_filenames_count_as_present(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    daily = repo / "archive" / "daily-summaries"
    daily.mkdir(parents=True)
    (daily / "2026-11-10-machine-b.md").write_text("x\n")
    (daily / "2026-11-11-machine-b.md").write_text("x\n")
    (daily / "notes.md").write_text("x\n")
    assert [g["date"] for g in pg.provenance_gaps(repo, "--since=2026-01-01")] == ["2026-11-12"]


def test_no_daily_dir_means_every_date_is_a_gap(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert [g["date"] for g in pg.provenance_gaps(repo, "--since=2026-01-01")] == ["2026-11-10", "2026-11-11", "2026-11-12"]


def test_all_dates_covered_yields_no_gaps(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    daily = repo / "archive" / "daily-summaries"
    daily.mkdir(parents=True)
    for d in ("2026-11-10", "2026-11-11", "2026-11-12"):
        (daily / f"{d}.md").write_text("x\n")
    assert pg.provenance_gaps(repo, "--since=2026-01-01") == []


def test_read_set_equals_pages_changed_and_nothing_else() -> None:
    report = {"pages_changed": ["b.md", "a.md"], "dead_paths": [{"page": "z.md"}], "systems_without_page": ["q"]}
    assert pg.read_set(report) == ["a.md", "b.md"]
    assert pg.read_set({}) == []


def test_one_git_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    calls = []
    real = pg.run_git

    def counting(args, **kw):
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(pg, "run_git", counting)
    pg.build(repo, {"pages_changed": []}, "--since=2026-01-01")
    assert len(calls) == 1


def test_window_that_is_not_a_date_bound_is_refused_before_git_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for bad in ("--output=leak.txt", "--all", "-n1"):
        with pytest.raises(ValueError):
            pg.commit_dates(repo, bad)
    assert not (repo / "leak.txt").exists()


def test_main_prints_json_and_refuses_without_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    assert pg.main(["--repo-root", str(repo), "--report", str(repo / "nope.json")]) == 2
    rep = repo / "r.json"
    rep.write_text(json.dumps({"pages_changed": ["p.md"]}))
    assert pg.main(["--repo-root", str(repo), "--report", str(rep), "--window=--since=2026-01-01"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["read_set"] == ["p.md"] and len(out["provenance_gaps"]) == 3
