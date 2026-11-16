"""dispatch.land_run's grind mode: one commit over finished rows, drops, trailers, ledgers, token."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_grind, run_token
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261015T000000Z"
PROFILE = "bug"
EVIDENCE = f"state/queue-grind/{PROFILE}/evidence/{RUN}/r1.done.md"
OTHER_EVIDENCE = f"state/queue-grind/{PROFILE}/evidence/20200101T000000Z/r1.done.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
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
    for name in ("a.py", "b.py", "other.py"):
        write_seen_by_git(root / name, "x = 2\n")
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


def test_two_rows_land_as_one_commit_with_two_trailers(repo) -> None:
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


def test_code_landing_with_zero_verdicts_carries_the_post_landing_stamp(repo) -> None:
    out = _call(repo, [_row("r1", ["a.py"])])
    assert out["committed"] is True
    body = _message(repo)
    assert f"Ledger-Receipt: {land_run_grind.POST_LANDING_RECEIPT}" in body
    assert "post-landing (grind exemption, DR-grind-review-runs-after-landing)" in body
    assert body.count("Ledger-Receipt:") == 1
    assert "Ledger-Receipt: review: met" not in body


def test_docs_only_landing_carries_no_review_receipt(repo) -> None:
    write_seen_by_git(repo / "notes.md", "# n\n")
    out = _call(repo, [_row("r1", ["notes.md"])])
    assert out["committed"] is True
    assert "Ledger-Receipt" not in _message(repo)


def test_mixed_docs_and_code_landing_carries_the_post_landing_stamp(repo) -> None:
    write_seen_by_git(repo / "notes.md", "# n\n")
    out = _call(repo, [_row("r1", ["notes.md", "a.py"])])
    assert out["committed"] is True
    assert _head_files(repo) == {"notes.md", "a.py"}
    assert _message(repo).count(f"Ledger-Receipt: {land_run_grind.POST_LANDING_RECEIPT}") == 1


def test_code_deletion_only_landing_carries_the_post_landing_stamp(repo) -> None:
    (repo / "other.py").unlink()
    out = _call(repo, [_row("r1", [], ["other.py"])])
    assert out["committed"] is True
    assert _head_files(repo) == {"other.py"}
    assert f"Ledger-Receipt: {land_run_grind.POST_LANDING_RECEIPT}" in _message(repo)


def test_post_landing_receipt_names_an_existing_decision_record() -> None:
    import re

    dr_id = re.search(r"\b(DR-[\w-]+)\)", land_run_grind.POST_LANDING_RECEIPT).group(1)
    root = Path(__file__).resolve().parents[4]
    assert (root / "docs" / "decisions" / f"{dr_id}.md").is_file()


def test_undeclared_dirty_path_is_not_staged(repo) -> None:
    _call(repo, [_row("r1", ["a.py"])])
    assert _status(repo) == {"b.py": " M", "other.py": " M"}


def test_halted_still_lands_finished_rows_and_names_the_halt(repo) -> None:
    out = _call(repo, [_row("r1", ["a.py"])], grind={"halted": "budget-exhausted"})
    assert out["committed"] is True
    assert "budget-exhausted" in _message(repo)


def test_deliverable_id_rides_the_message(repo) -> None:
    _call(repo, [_row("r1", ["a.py"])], deliverable_id="dlv-x")
    assert "Deliverable-Id: dlv-x" in _message(repo)


def test_deletion_of_a_never_tracked_absent_path_is_already_true(repo) -> None:
    out = _call(repo, [_row("r1", ["a.py"], ["state/bug-backlog/r1.yaml"])])
    assert out["committed"] is True
    assert out["dropped"] == [] and out["stranded"] == []
    assert "Row-Unlanded" not in _message(repo)


def test_dropped_deletion_carries_an_unlanded_trailer(repo) -> None:
    (repo / "state" / "bug-backlog").mkdir(parents=True)
    write_seen_by_git(repo / "state" / "bug-backlog" / "r1.yaml", "id: r1\n")
    _git(["add", "state/bug-backlog/r1.yaml"], repo)
    _git(["commit", "-q", "-m", "track r1"], repo)
    out = _call(repo, [_row("r1", ["a.py"], ["state/bug-backlog/r1.yaml"])])
    assert out["committed"] is True
    drop = {"row_id": "r1", "path": "state/bug-backlog/r1.yaml", "reason": "deletion-git-cannot-see"}
    assert out["dropped"] == [drop] and out["stranded"] == [drop]
    assert _head_files(repo) == {"a.py"}
    assert "Row-Unlanded: r1 state/bug-backlog/r1.yaml deletion-git-cannot-see" in _message(repo)


def test_another_runs_ledger_root_path_is_dropped_named_and_stays_untracked(repo) -> None:
    evidence = repo / OTHER_EVIDENCE
    evidence.parent.mkdir(parents=True)
    evidence.write_text("done\n", encoding="utf-8")
    out = _call(repo, [_row("r1", ["a.py", OTHER_EVIDENCE])])
    assert out["committed"] is True
    assert {"row_id": "r1", "path": OTHER_EVIDENCE, "reason": "ledger-root"} in out["dropped"]
    assert f"Row-Unlanded: r1 {OTHER_EVIDENCE} ledger-root" in _message(repo)
    assert _status(repo)[OTHER_EVIDENCE] == "??"


def test_this_runs_evidence_rides_the_commit_instead_of_stranding(repo) -> None:
    evidence = repo / EVIDENCE
    evidence.parent.mkdir(parents=True)
    evidence.write_text("done\n", encoding="utf-8")
    out = _call(repo, [_row("r1", ["a.py", EVIDENCE])])
    assert out["committed"] is True
    assert out["dropped"] == [] and out["stranded"] == []
    assert EVIDENCE in _head_files(repo)
    assert "Row-Unlanded" not in _message(repo)


def test_dropped_path_of_a_row_that_lands_nothing_is_still_named(repo) -> None:
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", [OTHER_EVIDENCE])])
    assert out["committed"] is True
    assert [r["row_id"] for r in out["rows"]] == ["r1"]
    assert f"Row-Unlanded: r2 {OTHER_EVIDENCE} ledger-root" in _message(repo)
    assert out["stranded"] == [{"row_id": "r2", "path": OTHER_EVIDENCE, "reason": "ledger-root"}]


def test_tracked_deletion_lands_with_removal_phrase(repo) -> None:
    (repo / "state" / "backlog" / "tracked.yaml").unlink()
    out = _call(repo, [_row("r1", ["a.py"], ["state/backlog/tracked.yaml"])])
    assert out["committed"] is True
    assert "state/backlog/tracked.yaml" in _head_files(repo)
    assert "1 backlog rows removed to archive" in _git(["log", "-1", "--format=%s"], repo)
    assert out["dropped"] == []


def test_changed_path_gone_from_the_worktree_is_named_not_skipped(repo) -> None:
    (repo / "b.py").unlink()
    out = _call(repo, [_row("r1", ["b.py"]), _row("r2", ["a.py"])])
    assert out["committed"] is True
    assert _head_files(repo) == {"a.py"}
    assert [u["row_id"] for u in out["unlanded"]] == ["r1"]
    assert "Row-Unlanded: r1 b.py missing-from-worktree" in _message(repo)


@pytest.mark.parametrize("rows", [[], [_row("r1", [EVIDENCE])]])
def test_nothing_landable_commits_nothing_and_releases_the_token(repo, rows) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo, rows, run_token=token)
    assert out["committed"] is False
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert run_token.admits(repo, RUN, token, "run.mjs")


def test_landed_commit_consumes_the_token(repo) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    assert _call(repo, [_row("r1", ["a.py"])], run_token=token)["committed"] is True
    assert not run_token.admits(repo, RUN, token, "run.mjs")


def test_wrong_token_is_refused_before_any_write(repo) -> None:
    run_token.mint(repo, RUN, "run.mjs")
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo, [_row("r1", ["a.py"])], run_token="not-the-token")
    assert out["refused"] == "run-token"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_grind_and_halted_by_together_are_refused(repo) -> None:
    out = _call(repo, [_row("r1", ["a.py"])], halted_by={"halted": "refusal", "reason": "x"})
    assert out["refused"] == "mode-ambiguous"
    assert _status(repo)["a.py"] == " M"


ARCHIVE = "archive/backlog/2026-10/tracked.yaml"


def _close_row(root: Path) -> None:
    """What ``grind_rows close`` leaves on disk: the queue file gone, an archive copy in its place."""
    (root / "state" / "backlog" / "tracked.yaml").unlink()
    (root / ARCHIVE).parent.mkdir(parents=True)
    (root / ARCHIVE).write_text("id: tracked\nstatus: done\n", encoding="utf-8")


def test_product_dirty_unlanded_row_keeps_its_queue_file_and_loses_its_archive_move(repo) -> None:
    _close_row(repo)
    (repo / "b.py").unlink()
    out = _call(repo, [
        _row("r1", ["b.py", ARCHIVE], ["state/backlog/tracked.yaml"]),
        _row("r2", ["a.py"]),
    ])
    assert out["committed"] is True
    assert [u["row_id"] for u in out["unlanded"]] == ["r1"]
    assert ARCHIVE not in _head_files(repo)
    assert "state/backlog/tracked.yaml" not in _head_files(repo)
    assert (repo / "state" / "backlog" / "tracked.yaml").read_text(encoding="utf-8") == "id: tracked\n"
    assert not (repo / ARCHIVE).exists()
    assert out["reopened"] == ["state/backlog/tracked.yaml"]


def test_product_dirty_landed_row_keeps_its_archive_move(repo) -> None:
    _close_row(repo)
    out = _call(repo, [_row("r1", ["a.py", ARCHIVE], ["state/backlog/tracked.yaml"])])
    assert out["committed"] is True
    assert {ARCHIVE, "state/backlog/tracked.yaml"} <= _head_files(repo)
    assert (repo / ARCHIVE).is_file()
    assert out["reopened"] == []


def test_landed_row_ledger_is_gone_and_unlanded_run_keeps_it(repo) -> None:
    ledger_dir = repo / "state" / "queue-grind" / PROFILE
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "r1.jsonl").write_text("{}\n", encoding="utf-8")
    (ledger_dir / "r9.jsonl").write_text("{}\n", encoding="utf-8")
    _call(repo, [_row("r9", [EVIDENCE])])
    assert (ledger_dir / "r9.jsonl").exists()
    _call(repo, [_row("r1", ["a.py"])])
    assert not (ledger_dir / "r1.jsonl").exists()


def test_repeat_call_returns_the_landed_result_without_a_second_commit(repo) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    first = _call(repo, [_row("r1", ["a.py"])], run_token=token)
    assert first["committed"] is True
    head = _git(["rev-parse", "HEAD"], repo)
    ledger_dir = repo / "state" / "queue-grind" / PROFILE
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "r1.jsonl").write_text("{}\n", encoding="utf-8")
    second = _call(repo, [_row("r1", ["a.py"])], run_token=token)
    assert second["committed"] is True and second["already_landed"] is True
    assert second["sha"] == first["sha"]
    assert [r["row_id"] for r in second["rows"]] == ["r1"]
    assert second["dropped"] == [] and second["unlanded"] == []
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert _status(repo)["b.py"] == " M"
    assert (ledger_dir / "r1.jsonl").exists()


def test_repeat_call_lands_rows_the_first_landing_missed_in_one_follow_up_commit(repo) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    first = _call(repo, [_row("r1", ["a.py"])], run_token=token)
    second = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])], run_token=token)
    assert second["committed"] is True
    assert second["sha"] != first["sha"] and second["sha"] == _git(["rev-parse", "HEAD"], repo).strip()
    assert [(r["row_id"], r["sha"]) for r in second["rows"]] == [("r1", first["sha"]), ("r2", second["sha"])]
    assert second["unlanded"] == []
    assert _head_files(repo) == {"b.py"}
    assert "Grind-Row: r2 fixed" in _message(repo) and "Grind-Row: r1" not in _message(repo)
    assert "b.py" not in _status(repo)
    third = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])], run_token=token)
    assert third["committed"] is False or third.get("already_landed") is True
    assert not run_token.admits(repo, RUN, token, "run.mjs")


def test_repeat_call_keeps_an_unfinished_late_row_open(repo) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    _call(repo, [_row("r1", ["a.py"])], run_token=token)
    (repo / "b.py").unlink()
    second = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])], run_token=token)
    assert [u["row_id"] for u in second["unlanded"]] == ["r2"]
    assert [r["row_id"] for r in second["rows"]] == ["r1"]


def test_repeat_call_with_a_wrong_token_is_still_refused(repo) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    _call(repo, [_row("r1", ["a.py"])], run_token=token)
    out = _call(repo, [_row("r1", ["a.py"])], run_token="not-the-token")
    assert out["refused"] == "run-token"
    assert "already_landed" not in out


def test_commit_grind_rows_is_the_in_process_entry(repo) -> None:
    params = {"run_id": RUN, "grind": {"profile": PROFILE, "rows": [_row("r1", ["a.py"])]}}
    out = land_run_grind.commit_grind_rows(repo, repo / ".git", params)
    assert out["committed"] is True and _head_files(repo) == {"a.py"}


def test_imageless_path_held_by_a_live_peer_is_dropped_and_the_rest_lands(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import grind_landing_select

    monkeypatch.setattr(grind_landing_select, "_live_peer_held", lambda _root, paths: {"a.py"} & set(paths))
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])])
    assert out["committed"] is True
    assert _head_files(repo) == {"b.py"}
    assert out["dropped"] == [{"row_id": "r1", "path": "a.py", "reason": "unattributed-shared"}]
    assert [u["row_id"] for u in out["unlanded"]] == ["r1"]
    assert "Row-Unlanded: r1 a.py unattributed-shared" in _message(repo)


def test_land_green_rows_makes_one_scoped_commit_per_row(repo) -> None:
    before = int(_git(["rev-list", "--count", "HEAD"], repo))
    params = {"run_id": RUN, "grind": {"profile": PROFILE, "rows": [_row("r1", ["a.py"]), _row("r2", ["b.py"])]}}
    out = land_run_grind.land_green_rows(repo, repo / ".git", params)
    assert out["committed"] is True
    assert int(_git(["rev-list", "--count", "HEAD"], repo)) == before + 2
    assert [c["row_id"] for c in out["commits"]] == ["r1", "r2"]
    assert out["commits"][0]["sha"] != out["commits"][1]["sha"] == out["sha"]
    assert _git(["show", "--name-only", "--format=", "HEAD~1"], repo).split() == ["a.py"]
    assert _head_files(repo) == {"b.py"}
    assert "Grind-Row: r2" in _message(repo) and "Grind-Row: r1" not in _message(repo)
    assert [r["row_id"] for r in out["rows"]] == ["r1", "r2"]
    assert _status(repo) == {"other.py": " M"}


def test_land_green_rows_stop_midway_leaves_only_unreached_rows_dirty(repo) -> None:
    (repo / "b.py").unlink()
    params = {"run_id": RUN, "grind": {"profile": PROFILE, "rows": [_row("r1", ["a.py"]), _row("r2", ["b.py"])]}}
    out = land_run_grind.land_green_rows(repo, repo / ".git", params)
    assert [c["row_id"] for c in out["commits"]] == ["r1"]
    assert [u["row_id"] for u in out["unlanded"]] == ["r2"]
    assert "a.py" not in _status(repo)


def test_land_green_rows_refuses_malformed_params(repo) -> None:
    out = land_run_grind.land_green_rows(repo, repo / ".git", {"run_id": RUN, "grind": {"profile": "", "rows": []}})
    assert out["committed"] is False and out["refused"] == "grind-params"


def _fix_row(row_id, paths):
    return {**_row(row_id, paths), "closed_by": "fix"}


def test_closed_by_fix_with_a_landed_sha_is_not_refused(repo) -> None:
    out = _call(repo, [_fix_row("r1", ["a.py"])])
    assert out["committed"] is True and "close_refused" not in out


def test_closed_by_fix_with_no_commit_is_refused_while_other_closers_pass(repo) -> None:
    flake = {**_row("r2", []), "closed_by": "flake"}
    out = _call(repo, [_fix_row("r1", []), flake, _row("r3", ["a.py"])])
    assert out["committed"] is True
    assert [c["row_id"] for c in out["close_refused"]] == ["r1"]
    assert "fix" in out["close_refused"][0]["reason"]


def test_closed_by_fix_dropped_path_is_refused_in_per_row_mode(repo) -> None:
    (repo / "b.py").unlink()
    params = {"run_id": RUN, "grind": {"profile": PROFILE, "rows": [_fix_row("r1", ["b.py"]), _fix_row("r2", ["a.py"])]}}
    out = land_run_grind.land_green_rows(repo, repo / ".git", params)
    assert [c["row_id"] for c in out["close_refused"]] == ["r1"]


def test_closed_by_fix_refused_when_nothing_commits(repo) -> None:
    out = _call(repo, [_fix_row("r1", [EVIDENCE])])
    assert out["committed"] is False
    assert [c["row_id"] for c in out["close_refused"]] == ["r1"]


def _no_tests_credit(tmp_path, reverify):
    from types import SimpleNamespace

    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit

    (tmp_path / "r.md").write_text(
        "PARTIAL: C1\nNot done: pytest not run; the likeness pytest fence bars the executor from running tests.\n",
        encoding="utf-8",
    )
    chunk = ChunkCommit(id="C1", title="t", paths=("a.py",), report="r.md")
    review = {
        "prep": {"slice_files": ["a.py"]}, "integration": {"unresolved": [], "confinement_violations": []},
        "reverify": reverify,
    }
    return land_run._grade(
        tmp_path, SimpleNamespace(chunks=(chunk,)), {"C1"}, review, None, None, set(), None, None, None, None, {},
    )


def test_not_run_reverify_never_reads_as_pass_but_credits_a_no_tests_partial(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import land_run

    not_run = {"status_before": "not_run", "failing": []}
    assert land_run._reverify_passed(not_run) is False  # never pass
    assert land_run._reverify_passed({**not_run, "status_after": "not_run"}) is False
    assert _no_tests_credit(tmp_path, not_run)["C1"].startswith("reverify-covered")
    assert _no_tests_credit(tmp_path, {**not_run, "failing": ["t.py::x"]}) == {}
    assert _no_tests_credit(tmp_path, {**not_run, "reverify_error": "boom"}) == {}
    assert _no_tests_credit(tmp_path, {"status_before": "indeterminate"}) == {}


def test_per_row_grind_through_the_entry_lands_one_commit_per_row(repo) -> None:
    before = int(_git(["rev-list", "--count", "HEAD"], repo))
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])], grind={"per_row": True})
    assert [c["row_id"] for c in out["commits"]] == ["r1", "r2"]
    assert int(_git(["rev-list", "--count", "HEAD"], repo)) == before + 2


def test_grind_without_per_row_still_lands_one_batch_commit(repo) -> None:
    before = int(_git(["rev-list", "--count", "HEAD"], repo))
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])])
    assert "commits" not in out
    assert int(_git(["rev-list", "--count", "HEAD"], repo)) == before + 1


def test_per_row_with_a_run_token_redeems_it_once_and_lands_every_row(repo) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    before = int(_git(["rev-list", "--count", "HEAD"], repo))
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])], grind={"per_row": True}, run_token=token)
    assert [c["row_id"] for c in out["commits"]] == ["r1", "r2"]
    assert int(_git(["rev-list", "--count", "HEAD"], repo)) == before + 2
    assert not run_token.admits(repo, RUN, token, "run.mjs")


def test_resumed_per_row_run_does_not_reland_a_committed_row(repo) -> None:
    rows = [_row("r1", ["a.py"]), _row("r2", ["b.py"])]
    _call(repo, rows, grind={"per_row": True})
    head = _git(["rev-parse", "HEAD"], repo)
    again = _call(repo, rows, grind={"per_row": True})
    assert again["committed"] is False and again["commits"] == []
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_resumed_per_row_run_on_a_spent_token_relands_nothing(repo) -> None:
    token = run_token.mint(repo, RUN, "run.mjs")
    rows = [_row("r1", ["a.py"]), _row("r2", ["b.py"])]
    _call(repo, rows, grind={"per_row": True}, run_token=token)
    head = _git(["rev-parse", "HEAD"], repo)
    _call(repo, rows, grind={"per_row": True}, run_token=token)
    assert _git(["rev-parse", "HEAD"], repo) == head
