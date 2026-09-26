
from __future__ import annotations

import multiprocessing
import os
import subprocess
from pathlib import Path
from typing import List

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.review_trail import reviewed_set as rs
from coordinator_core.win_portability import no_console_creationflags


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, encoding="utf-8", check=True,
        **no_console_creationflags(),
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _make_commit(repo: Path, message: str) -> str:
    _git(["commit", "--allow-empty", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def test_read_reviewed_set_empty_when_store_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    assert rs.read_reviewed_set(str(repo)) == frozenset()


def test_read_reviewed_set_sees_appended_shas_after_revalidation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sha = "a" * 40
    rs._append_shas(str(repo), {sha})
    assert sha in rs.read_reviewed_set(str(repo))


def test_read_reviewed_set_zero_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    rs._append_shas(str(repo), {"b" * 40})
    rs.read_reviewed_set(str(repo))

    def _forbidden(*_a, **_kw):  # pragma: no cover - only invoked on regression
        raise AssertionError("read_reviewed_set must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    result = rs.read_reviewed_set(str(repo))
    assert "b" * 40 in result


def test_read_reviewed_set_reflects_new_write_via_stat_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    first = rs.read_reviewed_set(str(repo))
    assert first == frozenset()
    rs._append_shas(str(repo), {"c" * 40})
    second = rs.read_reviewed_set(str(repo))
    assert second == frozenset({"c" * 40})


def test_fold_in_orders_sha_write_before_folded_id_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    sha = _make_commit(repo, "C1")

    result = rs.fold_in(str(repo), [("rec-1", f"{sha}^..{sha}")])
    assert result.folded_record_ids == ["rec-1"]
    assert result.unresolved_record_ids == []
    assert sha in rs.read_reviewed_set(str(repo))
    assert "rec-1" in rs.read_folded_record_ids(str(repo))


def test_interrupted_fold_self_heals_on_next_fold(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    sha = _make_commit(repo, "C1")
    sha_range = f"{sha}^..{sha}"

    rs._append_shas(str(repo), {sha})
    assert sha in rs.read_reviewed_set(str(repo))
    assert "rec-1" not in rs.read_folded_record_ids(str(repo))

    result = rs.fold_in(str(repo), [("rec-1", sha_range)])
    assert result.folded_record_ids == ["rec-1"]
    assert "rec-1" in rs.read_folded_record_ids(str(repo))
    assert rs.read_reviewed_set(str(repo)) == frozenset({sha})


def test_fold_in_accepts_non_ascii_record_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    sha = _make_commit(repo, "C1")
    record_id = "state/review-trail/Motörhead.md#3"

    result = rs.fold_in(str(repo), [(record_id, f"{sha}^..{sha}")])
    assert result.folded_record_ids == [record_id]
    assert result.unresolved_record_ids == []
    assert sha in rs.read_reviewed_set(str(repo))
    assert record_id in rs.read_folded_record_ids(str(repo))


def test_never_folds_record_id_before_its_shas_are_durable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sha = _make_commit(repo, "C1")

    call_order: List[str] = []
    real_append_shas = rs._append_shas
    real_append_ids = rs._append_folded_ids

    def _tracked_shas(repo_root, shas):
        call_order.append("shas")
        return real_append_shas(repo_root, shas)

    def _tracked_ids(repo_root, ids):
        call_order.append("ids")
        return real_append_ids(repo_root, ids)

    monkeypatch.setattr(rs, "_append_shas", _tracked_shas)
    monkeypatch.setattr(rs, "_append_folded_ids", _tracked_ids)

    rs.fold_in(str(repo), [("rec-1", f"{sha}^..{sha}")])
    assert call_order == ["shas", "ids"]


def test_abbreviated_sha_endpoint_leaves_record_unresolved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sha = _make_commit(repo, "C1")

    bogus_short = "0" * 7
    result = rs.fold_in(str(repo), [("rec-1", f"{bogus_short}..{sha}")])
    assert result.folded_record_ids == []
    assert result.unresolved_record_ids == ["rec-1"]
    assert result.new_shas == frozenset()
    assert "rec-1" not in rs.read_folded_record_ids(str(repo))
    assert rs.read_reviewed_set(str(repo)) == frozenset()


def test_malformed_caret_n_beyond_parents_leaves_record_unresolved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    sha = _make_commit(repo, "C1")

    result = rs.fold_in(str(repo), [("rec-1", f"{sha}^2..{sha}")])
    assert result.folded_record_ids == []
    assert result.unresolved_record_ids == ["rec-1"]
    assert "rec-1" not in rs.read_folded_record_ids(str(repo))


def test_unreachable_but_present_endpoint_leaves_record_unresolved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _make_commit(repo, "C0")
    orphan = _make_commit(repo, "C1-orphan")
    _git(["reset", "--hard", base], repo)
    tip = _make_commit(repo, "C2")

    result = rs.fold_in(str(repo), [("rec-1", f"{orphan}..{tip}")])
    assert result.folded_record_ids == []
    assert result.unresolved_record_ids == ["rec-1"]
    assert "rec-1" not in rs.read_folded_record_ids(str(repo))


def test_resolvable_record_alongside_unresolvable_one_only_folds_the_good_one(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    sha = _make_commit(repo, "C1")
    bogus_short = "f" * 7

    result = rs.fold_in(
        str(repo),
        [
            ("rec-good", f"{sha}^..{sha}"),
            ("rec-bad", f"{bogus_short}..{sha}"),
        ],
    )
    assert result.folded_record_ids == ["rec-good"]
    assert result.unresolved_record_ids == ["rec-bad"]
    assert sha in rs.read_reviewed_set(str(repo))
    assert "rec-bad" not in rs.read_folded_record_ids(str(repo))


def test_reach_set_build_failure_leaves_every_record_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    sha = _make_commit(repo, "C1")

    monkeypatch.setattr(rs, "_build_reach_set", lambda repo_root: None)
    result = rs.fold_in(str(repo), [("rec-1", f"{sha}^..{sha}")])
    assert result.folded_record_ids == []
    assert result.unresolved_record_ids == ["rec-1"]
    assert result.new_shas == frozenset()
    assert "rec-1" not in rs.read_folded_record_ids(str(repo))
    assert rs.read_reviewed_set(str(repo)) == frozenset()


def test_reader_discards_torn_line(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    good = "1" * 40
    torn = "2" * 20
    path = rs._shas_path(str(repo))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write((good + "\n").encode("ascii"))
        f.write((torn + "\n").encode("ascii"))
    result = rs.read_reviewed_set(str(repo))
    assert result == frozenset({good})


def _worker_fold(repo_root: str, sha: str, record_id: str) -> None:
    rs._append_shas(repo_root, {sha})
    rs._append_folded_ids(repo_root, [record_id])


def test_multi_process_fold_parses_cleanly_and_unions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    shas = [f"{i}" * 40 for i in range(1, 9)]
    jobs = [(str(repo), sha, f"rec-{i}") for i, sha in enumerate(shas, start=1)]

    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker_fold, args=job) for job in jobs]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    result = rs.read_reviewed_set(str(repo))
    assert result == frozenset(shas)
    folded_ids = rs.read_folded_record_ids(str(repo))
    assert folded_ids == frozenset(f"rec-{i}" for i in range(1, 9))
