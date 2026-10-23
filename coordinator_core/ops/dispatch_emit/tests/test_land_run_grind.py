"""dispatch.land_run's grind mode: one commit over finished rows, drops, trailers, ledgers, token."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_grind, run_token
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261015T000000Z"
PROFILE = "bug"
EVIDENCE = f"state/queue-grind/{PROFILE}/evidence/{RUN}/r1.done.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for name in ("a.py", "b.py", "other.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")
    (root / "state" / "backlog").mkdir(parents=True)
    (root / "state" / "backlog" / "tracked.yaml").write_text("id: tracked\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    (root / "b.py").write_text("x = 2\n", encoding="utf-8")
    (root / "other.py").write_text("x = 2\n", encoding="utf-8")
    return root


def _row(row_id, paths, deleted=()):
    return {"row_id": row_id, "outcome": "fixed", "paths": list(paths), "deleted_paths": list(deleted)}


def _call(root: Path, rows, **extra) -> dict:
    grind = {"profile": PROFILE, "rows": rows, **extra.pop("grind", {})}
    params = {"script_path": "run.mjs", "run_id": RUN, "grind": grind, **extra}
    return land_run_grind.land(root, root / ".git", params)


def _status(root: Path) -> dict:
    out = _git(["status", "--porcelain", "-uall"], root)
    return {line[3:]: line[:2] for line in out.splitlines()}


def _head_files(root: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def _message(root: Path) -> str:
    return _git(["log", "-1", "--format=%B"], root)


def test_two_rows_land_as_one_commit_with_two_trailers(repo):
    before = int(_git(["rev-list", "--count", "HEAD"], repo))
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])])
    assert out["committed"] is True
    assert int(_git(["rev-list", "--count", "HEAD"], repo)) == before + 1
    assert _head_files(repo) == {"a.py", "b.py"}
    body = _message(repo)
    assert "Grind-Row: r1 fixed" in body and "Grind-Row: r2 fixed" in body
    assert "Row-Unlanded" not in body
    assert [(r["row_id"], r["sha"]) for r in out["rows"]] == [("r1", out["sha"]), ("r2", out["sha"])]
    assert out["dropped"] == [] and out["stranded"] == []


def test_undeclared_dirty_path_is_not_staged(repo):
    _call(repo, [_row("r1", ["a.py"])])
    assert _status(repo) == {"b.py": " M", "other.py": " M"}


def test_halted_still_lands_finished_rows_and_names_the_halt(repo):
    out = _call(repo, [_row("r1", ["a.py"])], grind={"halted": "budget-exhausted"})
    assert out["committed"] is True
    assert "budget-exhausted" in _message(repo)


def test_deliverable_id_rides_the_message(repo):
    _call(repo, [_row("r1", ["a.py"])], deliverable_id="dlv-x")
    assert "Deliverable-Id: dlv-x" in _message(repo)


def test_dropped_deletion_carries_an_unlanded_trailer(repo):
    out = _call(repo, [_row("r1", ["a.py"], ["state/bug-backlog/r1.yaml"])])
    assert out["committed"] is True
    drop = {"row_id": "r1", "path": "state/bug-backlog/r1.yaml", "reason": "deletion-git-cannot-see"}
    assert out["dropped"] == [drop] and out["stranded"] == [drop]
    assert _head_files(repo) == {"a.py"}
    assert "Row-Unlanded: r1 state/bug-backlog/r1.yaml deletion-git-cannot-see" in _message(repo)


def test_ledger_root_path_is_dropped_named_and_stays_untracked(repo):
    evidence = repo / EVIDENCE
    evidence.parent.mkdir(parents=True)
    evidence.write_text("done\n", encoding="utf-8")
    out = _call(repo, [_row("r1", ["a.py", EVIDENCE])])
    assert out["committed"] is True
    assert {"row_id": "r1", "path": EVIDENCE, "reason": "ledger-root"} in out["dropped"]
    assert f"Row-Unlanded: r1 {EVIDENCE} ledger-root" in _message(repo)
    assert _status(repo)[EVIDENCE] == "??"


def test_dropped_path_of_a_row_that_lands_nothing_is_still_named(repo):
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", [EVIDENCE])])
    assert out["committed"] is True
    assert [r["row_id"] for r in out["rows"]] == ["r1"]
    assert f"Row-Unlanded: r2 {EVIDENCE} ledger-root" in _message(repo)
    assert out["stranded"] == [{"row_id": "r2", "path": EVIDENCE, "reason": "ledger-root"}]


def test_tracked_deletion_lands_with_removal_phrase(repo):
    (repo / "state" / "backlog" / "tracked.yaml").unlink()
    out = _call(repo, [_row("r1", ["a.py"], ["state/backlog/tracked.yaml"])])
    assert out["committed"] is True
    assert "state/backlog/tracked.yaml" in _head_files(repo)
    assert "1 backlog rows removed to archive" in _git(["log", "-1", "--format=%s"], repo)
    assert out["dropped"] == []


def test_changed_path_gone_from_the_worktree_is_named_not_skipped(repo):
    (repo / "b.py").unlink()
    out = _call(repo, [_row("r1", ["a.py", "b.py"])])
    assert out["committed"] is True
    assert _head_files(repo) == {"a.py"}
    assert "Row-Unlanded: r1 b.py missing-from-worktree" in _message(repo)


@pytest.mark.parametrize("rows", [[], [_row("r1", [EVIDENCE])]])
def test_nothing_landable_commits_nothing_and_releases_the_token(repo, rows):
    token = run_token.mint(repo, RUN, "run.mjs")
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo, rows, run_token=token)
    assert out["committed"] is False
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert run_token.admits(repo, RUN, token, "run.mjs")


def test_landed_commit_consumes_the_token(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    assert _call(repo, [_row("r1", ["a.py"])], run_token=token)["committed"] is True
    assert not run_token.admits(repo, RUN, token, "run.mjs")


def test_wrong_token_is_refused_before_any_write(repo):
    run_token.mint(repo, RUN, "run.mjs")
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo, [_row("r1", ["a.py"])], run_token="not-the-token")
    assert out["refused"] == "run-token"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_grind_and_halted_by_together_are_refused(repo):
    out = _call(repo, [_row("r1", ["a.py"])], halted_by={"halted": "refusal", "reason": "x"})
    assert out["refused"] == "mode-ambiguous"
    assert _status(repo)["a.py"] == " M"


def test_landed_row_ledger_is_gone_and_unlanded_run_keeps_it(repo):
    ledger_dir = repo / "state" / "queue-grind" / PROFILE
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "r1.jsonl").write_text("{}\n", encoding="utf-8")
    (ledger_dir / "r9.jsonl").write_text("{}\n", encoding="utf-8")
    _call(repo, [_row("r9", [EVIDENCE])])
    assert (ledger_dir / "r9.jsonl").exists()
    _call(repo, [_row("r1", ["a.py"])])
    assert not (ledger_dir / "r1.jsonl").exists()


def test_repeat_call_returns_the_landed_result_without_a_second_commit(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    first = _call(repo, [_row("r1", ["a.py"])], run_token=token)
    assert first["committed"] is True
    head = _git(["rev-parse", "HEAD"], repo)
    ledger_dir = repo / "state" / "queue-grind" / PROFILE
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "r1.jsonl").write_text("{}\n", encoding="utf-8")
    second = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])], run_token=token)
    assert second["committed"] is True and second["already_landed"] is True
    assert second["sha"] == first["sha"]
    assert [r["row_id"] for r in second["rows"]] == ["r1"]
    assert second["dropped"] == []
    assert [u["row_id"] for u in second["unlanded"]] == ["r2"]
    assert first["sha"] in second["unlanded"][0]["reason"]
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert _status(repo)["b.py"] == " M"
    assert (ledger_dir / "r1.jsonl").exists()


def test_repeat_call_with_a_wrong_token_is_still_refused(repo):
    token = run_token.mint(repo, RUN, "run.mjs")
    _call(repo, [_row("r1", ["a.py"])], run_token=token)
    out = _call(repo, [_row("r1", ["a.py"])], run_token="not-the-token")
    assert out["refused"] == "run-token"
    assert "already_landed" not in out


def test_commit_grind_rows_is_the_in_process_entry(repo):
    params = {"run_id": RUN, "grind": {"profile": PROFILE, "rows": [_row("r1", ["a.py"])]}}
    out = land_run_grind.commit_grind_rows(repo, repo / ".git", params)
    assert out["committed"] is True and _head_files(repo) == {"a.py"}


def test_imageless_path_held_by_a_live_peer_is_dropped_and_the_rest_lands(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import grind_landing_select

    monkeypatch.setattr(grind_landing_select, "_live_peer_held", lambda _root, paths: {"a.py"} & set(paths))
    out = _call(repo, [_row("r1", ["a.py", "b.py"])])
    assert out["committed"] is True
    assert _head_files(repo) == {"b.py"}
    assert out["dropped"] == [{"row_id": "r1", "path": "a.py", "reason": "unattributed-shared"}]
    assert "Row-Unlanded: r1 a.py unattributed-shared" in _message(repo)
