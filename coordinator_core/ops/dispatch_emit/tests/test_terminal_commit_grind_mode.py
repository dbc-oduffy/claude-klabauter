"""dispatch.terminal_commit's grind mode: one commit over finished rows, drops, stamps, ledgers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
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
    return terminal_commit._handler(params, repo_root=root / ".git")


def _status(root: Path) -> dict:
    out = _git(["status", "--porcelain", "-uall"], root)
    return {line[3:]: line[:2] for line in out.splitlines()}


def _head_files(root: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def test_two_rows_land_as_one_commit_with_trailers_and_stamps(repo):
    before = _git(["rev-list", "--count", "HEAD"], repo).strip()
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])])
    assert out["committed"] is True
    assert int(_git(["rev-list", "--count", "HEAD"], repo)) == int(before) + 1
    assert _head_files(repo) == {"a.py", "b.py"}
    body = _git(["log", "-1", "--format=%B"], repo)
    assert "Grind-Row: r1 fixed" in body and "Grind-Row: r2 fixed" in body
    assert [(r["row_id"], r["sha"]) for r in out["rows"]] == [("r1", out["sha"]), ("r2", out["sha"])]
    assert out["dropped"] == []


def test_halted_still_lands_finished_rows_and_names_the_halt(repo):
    out = _call(repo, [_row("r1", ["a.py"])], grind={"halted": "budget-exhausted"})
    assert out["committed"] is True
    assert "budget-exhausted" in _git(["log", "-1", "--format=%B"], repo)


def test_untracked_deletion_is_dropped_not_refused(repo):
    out = _call(repo, [_row("r1", ["a.py"], ["state/bug-backlog/r1.yaml"])])
    assert out["committed"] is True
    assert out["dropped"] == [
        {"row_id": "r1", "path": "state/bug-backlog/r1.yaml", "reason": "deletion-git-cannot-see"}
    ]
    assert _head_files(repo) == {"a.py"}


def test_ledger_root_path_is_dropped_and_stays_untracked(repo):
    evidence = repo / EVIDENCE
    evidence.parent.mkdir(parents=True)
    evidence.write_text("done\n", encoding="utf-8")
    out = _call(repo, [_row("r1", ["a.py", EVIDENCE])])
    assert out["committed"] is True
    assert {"row_id": "r1", "path": EVIDENCE, "reason": "ledger-root"} in out["dropped"]
    assert _status(repo)[EVIDENCE] == "??"


def test_tracked_deletion_lands_with_removal_phrase(repo):
    (repo / "state" / "backlog" / "tracked.yaml").unlink()
    out = _call(repo, [_row("r1", ["a.py"], ["state/backlog/tracked.yaml"])])
    assert out["committed"] is True
    assert "state/backlog/tracked.yaml" in _head_files(repo)
    assert "1 backlog rows removed to archive" in _git(["log", "-1", "--format=%s"], repo)
    assert out["dropped"] == []


def test_undeclared_dirty_path_stays_uncommitted(repo):
    _call(repo, [_row("r1", ["a.py"])])
    assert _status(repo) == {"b.py": " M", "other.py": " M"}


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
