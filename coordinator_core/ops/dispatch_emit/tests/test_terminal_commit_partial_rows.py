"""dispatch.terminal_commit lands an executor-DONE row's reviewed files (delivery FAIL) with the review trailer, withholds an executor-PARTIAL row and surfaces it as a blocker, leaves a
BLOCKED or never-started row stranded, and refuses an empty subject."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 3, "fixes": 3}


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
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _run(repo: Path, report_text: str | None) -> str:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    report = ""
    if report_text is not None:
        report = "report-C1.md"
        (repo / report).write_text(report_text, encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="wire rate", paths=("a.py",), report=report),)
    )
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


def _call(repo: Path, script: str) -> dict:
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": ["C1"], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def test_delivered_row_with_failed_delivery_lands_with_trailer_and_stays_incomplete(repo):
    out = _call(repo, _run(repo, "DONE: wired; rebuild AC needs a UE run\n"))
    assert out["committed"] is True, out
    assert out["partial_committed"] == ["C1"]
    assert out["chunks_committed"] == []
    assert out["stranded"] == {}
    assert "blockers" not in out
    assert out.get("rows_coded", {}) == {}
    assert "plan_status" not in out
    body = _git(["show", "--stat", "--format=%s%n%b", "HEAD"], repo)
    assert body.startswith("C1: wire rate")
    assert "Inline-Review: applies rev-stem -- execute-review: 3 slices, 3 fixes" in body
    assert "a.py" in body
    assert _git(["status", "--porcelain", "--", "a.py"], repo) == ""


def test_executor_partial_row_is_withheld_and_surfaced_as_blocker(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(
        repo,
        _run(repo, "PARTIAL: edits done\nNot done: `rebuild-plugin` rc=0 AC, EM must run it\n"),
    )
    assert out["nothing_to_commit"] is True
    assert out["stranded"] == {"C1": ["a.py"]}
    assert "rebuild-plugin" in out["blockers"]["C1"]
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert "a.py" not in _git(["ls-files"], repo)


@pytest.mark.parametrize("report", ["BLOCKED: no runtime\n", None])
def test_blocked_or_never_started_row_stays_stranded(repo, report):
    out = _call(repo, _run(repo, report))
    assert out["nothing_to_commit"] is True
    assert out["stranded"] == {"C1": ["a.py"]}
    assert "blockers" not in out
    assert "a.py" not in _git(["ls-files"], repo)


def test_empty_subject_is_refused_before_any_commit(repo, monkeypatch):
    monkeypatch.setattr(terminal_commit, "_subject", lambda contributing, fallback: ":")
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo, _run(repo, "DONE: x\n"))
    assert out["committed"] is False
    assert out["refused"] == "empty-subject"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_subject_without_chunks_names_the_review_stem():
    assert terminal_commit._subject([], "review trail: s") == "review trail: s"
    chunk = ChunkCommit(id="C1", title=" ", paths=("a.py",))
    assert terminal_commit._subject([chunk], "x") == "C1"


def test_every_incomplete_id_carries_a_reason(repo):
    out = _call(
        repo,
        _run(repo, "PARTIAL: edits done\nNot done: `rebuild-plugin` rc=0 AC, EM must run it\n"),
    )
    assert out["incomplete_reasons"]["C1"].startswith("executor_partial: ")
    assert "rebuild-plugin" in out["incomplete_reasons"]["C1"]


def test_blocked_row_reason_is_incomplete_unreported(repo):
    out = _call(repo, _run(repo, "BLOCKED: no runtime\n"))
    assert out["incomplete_reasons"] == {"C1": "incomplete_unreported"}


def test_stranded_row_reply_names_the_next_command(repo):
    out = _call(repo, _run(repo, "BLOCKED: no runtime\n"))
    assert out["next"] == (
        "stranded files for C1: commit or discard them, "
        "then re-emit with emit-dispatch-workflow --plan <plan> --only-incomplete <run-output-file>"
    )


def test_resume_hint_names_the_plan_and_every_stranded_row():
    hint = terminal_commit._resume_hint(
        {"P1": ["a.py"], "X1": ["b.py"]},
        {},
        {"plan_path": "docs/plans/p.md", "task_output_path": "out/task.output"},
    )
    assert hint == (
        "stranded files for P1, X1: commit or discard them, then re-emit with "
        "emit-dispatch-workflow --plan docs/plans/p.md --only-incomplete out/task.output"
    )


_STATUS_FIELD_PARTIAL = (
    "# API row report\n\nstatus: PARTIAL\n\nExamined 6 of 6 footprint paths.\n\n"
    "## Delivered\n- routes\n\n"
    "## Why PARTIAL: the row's Done-when cannot go green inside the footprint\n"
    "contracts.test.ts fails only because two files OUTSIDE my footprint lack rows.\n"
    "1. `contracts.test.ts` SAMPLES needs samples.\n\n## Concerns\n- gate.ts\n"
)


def test_status_field_partial_row_is_withheld_with_its_cause_section(repo):
    out = _call(repo, _run(repo, _STATUS_FIELD_PARTIAL))
    assert out["nothing_to_commit"] is True
    assert out["stranded"] == {"C1": ["a.py"]}
    assert "OUTSIDE my footprint" in out["blockers"]["C1"]
    assert out["incomplete_reasons"]["C1"].startswith("executor_partial: ")


@pytest.mark.parametrize("line", ["status: DONE", "**Status:** DONE_WITH_CONCERNS", "## status: done"])
def test_status_field_done_row_lands(repo, line):
    out = _call(repo, _run(repo, f"# report\n\n{line}\n\nbody\n"))
    assert out["partial_committed"] == ["C1"], out


@pytest.mark.parametrize("line", ["status: BLOCKED", "Status: partial"])
def test_status_field_not_done_is_not_delivered(line):
    text = f"# report\n\n{line}\n"
    assert terminal_commit._UNDELIVERED_REPORT_RE.search(text)
    assert not terminal_commit._DELIVERED_REPORT_RE.search(text)


def test_undone_summary_prefers_not_done_lines_over_the_cause_section():
    text = _STATUS_FIELD_PARTIAL + "Not done: rebuild\n"
    assert terminal_commit._undone_summary(text) == "Not done: rebuild"


_GATE_PARTIAL = (
    "PARTIAL: edits done\n"
    "gate-blocker: outside-footprint: src/sibling.ts(3,1): error TS2304\n"
)


def _passed_review(slice_files=("a.py",), **over):
    review = {
        **_REVIEWED,
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [],
                 "slice_files": list(slice_files)},
        "delivery": {"verdict": "PASS", "product_files": 1},
        "integration": {"sidecar": "s", "unresolved": [], "confinement_violations": 0},
    }
    review.update(over)
    return review


def _call_with(repo: Path, script: str, review: dict) -> dict:
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": ["C1"], "inline_review": review},
        repo_root=repo / ".git",
    )


def test_gate_blocked_partial_row_in_the_reviewed_slice_commits_and_regrades(repo):
    out = _call_with(repo, _run(repo, _GATE_PARTIAL), _passed_review())
    assert out["committed"] is True, out
    assert out["chunks_committed"] == ["C1"]
    assert out["partial_committed"] == []
    assert out["stranded"] == {}
    assert out["regraded"]["C1"].startswith("outside-footprint: src/sibling.ts")
    assert out["incomplete_chunks"] == []
    assert "incomplete_reasons" not in out
    assert _git(["status", "--porcelain", "--", "a.py"], repo) == ""


@pytest.mark.parametrize(
    "review",
    [
        _passed_review(slice_files=("other.py",)),
        _passed_review(delivery={"verdict": "FAIL", "product_files": 1}),
        _passed_review(integration={"sidecar": "s", "unresolved": ["F1"], "confinement_violations": 0}),
        _passed_review(integration={"sidecar": "s", "unresolved": [], "confinement_violations": 2}),
        _REVIEWED,
    ],
    ids=["file-outside-slice", "delivery-fail", "unresolved", "confinement", "no-prep"],
)
def test_gate_blocked_partial_row_stays_withheld_without_a_passing_covering_review(repo, review):
    out = _call_with(repo, _run(repo, _GATE_PARTIAL), review)
    assert out["nothing_to_commit"] is True
    assert out["stranded"] == {"C1": ["a.py"]}
    assert "regraded" not in out
    assert "a.py" not in _git(["ls-files"], repo)


def test_partial_row_without_the_gate_blocker_line_is_not_regraded(repo):
    prose = "PARTIAL: edits done\nNot done: tsc red from files OUTSIDE my footprint\n"
    out = _call_with(repo, _run(repo, prose), _passed_review())
    assert out["nothing_to_commit"] is True
    assert "regraded" not in out
