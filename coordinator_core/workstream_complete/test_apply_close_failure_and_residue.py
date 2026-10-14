"""apply() folds every non-success downgrade into `failed` and measures what
the session leaves behind. Every repo is a real git repo under tmp_path; the
close commit call itself is stubbed so each case fixes the commit outcome."""

import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

import pytest

from coordinator_core.ceremony_common import apply_halt
from coordinator_core.session import scope
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import apply as ws_apply

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SID = "sid-mine"
EXIT = ws_apply.WorkstreamApplyExitCode


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags())


class _CommitResult:
    def __init__(self, *, commit_failed: bool = False, committed_sha: Optional[str] = "deadbeef") -> None:
        self.commit_failed = commit_failed
        self.committed_sha = committed_sha
        self.pushed = False
        self.integrity_breach = False
        self.diagnostics: tuple = ()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "tracked.txt"], tmp_path)
    _git(["commit", "-q", "-m", "seed"], tmp_path)
    return tmp_path


def _run_apply(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    decisions: dict[str, Any],
    *,
    commit: Optional[_CommitResult] = None,
    directives: Optional[list[dict[str, Any]]] = None,
    entry_stdout: str = "",
    gates: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    from coordinator_core.ops.ceremony import post_commit_tail

    result = commit or _CommitResult()
    monkeypatch.setattr(
        ws_apply.directives_commit_tail,
        "run_close_commit_and_release_claims",
        lambda worktree_root, **kwargs: result,
    )
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda decisions, root: None)
    monkeypatch.setattr(post_commit_tail, "fold_completion_entry_commit", lambda *a, **k: {"acted": []})

    def entry_main(argv: list[str]) -> int:
        print(entry_stdout)
        return 0

    entry_module = ModuleType("fake_entry")
    entry_module.main = entry_main
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: entry_module)
    envelope = {
        "artifact": {"path": str(root)},
        "directives": directives or [],
        "judgment_points": [],
        "decisions": decisions,
        "preflight": {"session_shape": {"sid": SID}},
        "gates": gates or {},
    }
    monkeypatch.setattr(ws_apply, "brief", lambda decisions=None: envelope)
    return ws_apply.apply(decisions=decisions)


def _failed_ids(report: dict[str, Any]) -> list[str]:
    return [entry["id"] for entry in report["failed"]]


def _assert_exit_ladder_invariant(exit_code: int, report: dict[str, Any]) -> None:
    if exit_code in (int(EXIT.DIRECTIVE_FAILED), int(EXIT.PARTIAL_MUTATION)):
        assert report["failed"], f"exit {exit_code} with failed: []"


# --- hole 1: a close commit that did not land --------------------------------


def test_no_sha_commit_with_a_dirty_stage_path_is_a_failure(monkeypatch, repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    exit_code, report = _run_apply(
        monkeypatch, repo, {"subject": "s", "stage_paths": ["tracked.txt"]}, commit=_CommitResult(committed_sha=None)
    )
    assert exit_code == int(EXIT.PARTIAL_MUTATION)
    assert "close-commit:not-landed" in _failed_ids(report)
    assert report["close_commit"]["not_landed"] == ["tracked.txt"]
    assert "already_landed" not in report["close_commit"]
    _assert_exit_ladder_invariant(exit_code, report)


def test_no_sha_commit_whose_stage_paths_match_head_is_an_idempotent_reclose(monkeypatch, repo: Path) -> None:
    exit_code, report = _run_apply(
        monkeypatch, repo, {"subject": "s", "stage_paths": ["tracked.txt"]}, commit=_CommitResult(committed_sha=None)
    )
    assert exit_code == int(EXIT.SUCCESS)
    assert report["failed"] == []
    assert report["close_commit"]["already_landed"] is True
    assert "not_landed" not in report["close_commit"]


def test_commit_failed_is_a_failure(monkeypatch, repo: Path) -> None:
    exit_code, report = _run_apply(
        monkeypatch, repo, {"subject": "s"}, commit=_CommitResult(commit_failed=True, committed_sha=None)
    )
    assert exit_code == int(EXIT.PARTIAL_MUTATION)
    assert _failed_ids(report) == ["close-commit:commit-failed"]
    _assert_exit_ladder_invariant(exit_code, report)


def test_a_skipped_commit_is_not_a_failure_in_itself(monkeypatch, repo: Path) -> None:
    exit_code, report = _run_apply(monkeypatch, repo, {})
    assert report["close_commit"]["attempted"] is False
    assert exit_code == int(EXIT.SUCCESS)
    assert report["failed"] == []


def test_a_completion_entry_left_untracked_is_a_failure(monkeypatch, repo: Path) -> None:
    entry_rel = "state/completions/entry.md"
    (repo / entry_rel).parent.mkdir(parents=True)
    (repo / entry_rel).write_text("entry\n", encoding="utf-8")
    directive = {
        "id": "d-complete-entry",
        "cli": "coordinator-complete-entry",
        "args": [],
        "depends_on": None,
        "already_satisfied": False,
    }
    exit_code, report = _run_apply(
        monkeypatch, repo, {"subject": "s"}, directives=[directive], entry_stdout=entry_rel
    )
    assert exit_code == int(EXIT.PARTIAL_MUTATION)
    assert "completion-entry:untracked" in _failed_ids(report)
    _assert_exit_ladder_invariant(exit_code, report)


def test_a_pre_mutation_budget_breach_names_itself_in_failed(monkeypatch, repo: Path) -> None:
    monkeypatch.setattr(ws_apply, "budget_check_pre_mutation", lambda budget: "composition budget breach: test")
    exit_code, report = _run_apply(monkeypatch, repo, {"subject": "s"})
    assert exit_code == int(EXIT.DIRECTIVE_FAILED)
    assert _failed_ids(report) == ["budget-breach:pre-mutation"]
    assert "budget_breach" in report
    _assert_exit_ladder_invariant(exit_code, report)


# --- hole 2: residue the session still owns ----------------------------------


def test_an_owned_dirty_file_left_out_of_the_commit_is_residue(monkeypatch, repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    scope.touch(SID, "tracked.txt", cwd=str(repo), root=str(repo))
    exit_code, report = _run_apply(monkeypatch, repo, {"subject": "s", "stage_paths": []})

    assert exit_code == int(EXIT.PARTIAL_MUTATION)
    assert "close-residue:owned-uncommitted" in _failed_ids(report)
    entry = next(e for e in report["failed"] if e["id"] == "close-residue:owned-uncommitted")
    assert entry["paths"] == ["tracked.txt"]
    verdict = report["completion_verdict"]
    assert verdict["verdict"] != "complete"
    assert verdict["verdict"] == "incomplete"
    assert any(item["gate"] == "owned_uncommitted" and item["reference"] == "tracked.txt" for item in verdict["residue"])
    assert "post_close" not in report
    _assert_exit_ladder_invariant(exit_code, report)


def test_an_owned_file_the_commit_landed_is_not_residue(monkeypatch, repo: Path) -> None:
    scope.touch(SID, "tracked.txt", cwd=str(repo), root=str(repo))
    exit_code, report = _run_apply(monkeypatch, repo, {"subject": "s", "stage_paths": ["tracked.txt"]})
    assert exit_code == int(EXIT.SUCCESS)
    assert report["failed"] == []
    assert report["completion_verdict"]["verdict"] == "complete"


def test_a_held_in_flight_baton_with_no_stamp_is_residue(monkeypatch, repo: Path) -> None:
    baton = "2026-10-14-run.md"
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / baton
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text(SID, encoding="utf-8")
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / baton).write_text("---\ndeployment_state: in_flight\n---\n\nbody\n", encoding="utf-8")

    exit_code, report = _run_apply(monkeypatch, repo, {"subject": "s"})

    assert exit_code == int(EXIT.PARTIAL_MUTATION)
    entry = next(e for e in report["failed"] if e["id"] == "close-residue:unstamped-baton")
    assert entry["batons"] == [baton]
    assert report["completion_verdict"]["verdict"] == "incomplete"
    _assert_exit_ladder_invariant(exit_code, report)


def test_residue_keeps_an_existing_non_success_exit(monkeypatch, repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    scope.touch(SID, "tracked.txt", cwd=str(repo), root=str(repo))
    monkeypatch.setattr(ws_apply, "_execute_directives", lambda *a, **k: (int(EXIT.HALTED_AT_JUDGMENT), {
        "landed": [], "blocked": ["jp"], "blocked_remedy": {}, "failed": [], "degraded": [], "results": [],
    }))
    exit_code, report = _run_apply(monkeypatch, repo, {"subject": "s", "stage_paths": []})
    assert exit_code == int(EXIT.HALTED_AT_JUDGMENT)
    assert "close-residue:owned-uncommitted" in _failed_ids(report)


def test_the_four_census_gates_stay_advisory(monkeypatch, repo: Path) -> None:
    gates = {"completeness_checklist": {"items": [{"status": "open", "title": "t", "reference": "r"}]}}
    exit_code, report = _run_apply(monkeypatch, repo, {"subject": "s"}, gates=gates)
    assert exit_code == int(EXIT.SUCCESS)
    assert report["failed"] == []
    assert any(r["gate"] == "completeness_checklist" for r in report["completion_verdict"]["readings"])


def test_the_halt_ladder_is_unchanged() -> None:
    assert {m.name: int(m) for m in EXIT if m.name != "SUCCESS"} == dict(apply_halt.CEREMONY_HALT_EXIT_CODES)
