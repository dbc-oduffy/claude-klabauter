
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.distill import delete_guard
from coordinator_core.distill._common import active_reference_guard
from coordinator_core.frontmatter.primitives import serialize_yaml_scalar
from coordinator_core.distill.delete_guard import (
    DeleteCandidate,
    check_active_reference,
    check_commitment_closure,
    check_distill_fate,
    check_harvest_provenance,
    check_realized_by,
    check_shipped_in,
    check_status_actioned,
    evaluate_candidate,
    evaluate_candidate_detailed,
    resolve_realized_by,
)
from coordinator_core.ops.distill_disposal_manifest import evaluate_candidate_receipts
from coordinator_core.win_portability import no_console_creationflags

_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")

# real `git log`-derived commit dates to compare against DISTILL_FATE_STAMPING_CUTOVER.
# tests sharing a repo. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    (repo_root / "committed.txt").write_text("hello\n", encoding="utf-8")
    _git(repo_root, "add", "committed.txt")
    _git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def _commit_sha(repo_root: Path) -> str:
    result = _git(repo_root, "rev-parse", "HEAD")
    return result.stdout.strip()


def test_shipped_in_present_passes():
    result = check_shipped_in("shipped_in: 68b27420\nstatus: actioned\n")
    assert result.passed is True


def test_shipped_in_absent_blocks():
    result = check_shipped_in("status: actioned\n")
    assert result.passed is False
    assert "absent" in result.detail


def test_status_actioned_passes():
    result = check_status_actioned("status: actioned\n")
    assert result.passed is True


def test_status_active_blocks():
    result = check_status_actioned("status: active\n")
    assert result.passed is False
    assert "active" in result.detail


@_requires_rg
def test_active_reference_still_referenced_blocks(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("see candidate-name.md for context\n", encoding="utf-8")
    result = check_active_reference("candidate-name.md", tmp_path)
    assert result.passed is False


@_requires_rg
def test_active_reference_not_referenced_passes(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("nothing relevant here\n", encoding="utf-8")
    result = check_active_reference("totally-unreferenced-slug.md", tmp_path)
    assert result.passed is True


_NEEDLE = "cross-repo/archive/2026-07-01-some-memo.md"


def _write_commitment(tmp_path: Path, name: str, body: str) -> Path:
    commitments_dir = tmp_path / "state" / "cross-repo-commitments"
    commitments_dir.mkdir(parents=True, exist_ok=True)
    entry = commitments_dir / name
    entry.write_text(body, encoding="utf-8")
    return entry


def test_commitment_closure_absent_surface_fails_loud(tmp_path: Path):
    assert not (tmp_path / "state" / "cross-repo-commitments").exists()
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is False
    assert result.detail == "commitment-closure: surface absent"


def test_commitment_closure_never_silently_passes_when_absent(tmp_path: Path):
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is not True


def test_commitment_closure_empty_surface_passes(tmp_path: Path):
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True)
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is True


def test_commitment_closure_open_commitment_referencing_candidate_blocks(tmp_path: Path):
    _write_commitment(
        tmp_path,
        "2026-07-01-open-entry.yaml",
        f"title: pending obligation\nstatus: open\nmemo: {_NEEDLE}\n",
    )
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is False
    assert "open commitment" in result.detail
    assert "2026-07-01-open-entry.yaml" in result.detail


def test_commitment_closure_closed_commitment_referencing_candidate_passes(tmp_path: Path):
    _write_commitment(
        tmp_path,
        "2026-07-01-closed-entry.yaml",
        f"title: resolved obligation\nstatus: closed\nmemo: {_NEEDLE}\n",
    )
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is True


def test_commitment_closure_open_commitment_not_referencing_candidate_passes(tmp_path: Path):
    _write_commitment(
        tmp_path,
        "2026-07-01-unrelated-entry.yaml",
        "title: unrelated obligation\nstatus: open\nmemo: cross-repo/archive/other-memo.md\n",
    )
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is True


def test_commitment_closure_unparseable_entry_fails_closed(tmp_path: Path):
    _write_commitment(
        tmp_path,
        "2026-07-01-broken-entry.yaml",
        "title: [unclosed bracket\nstatus: open\n  bad:\n indent\n",
    )
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is False
    assert "unparseable" in result.detail
    assert "2026-07-01-broken-entry.yaml" in result.detail


def test_commitment_closure_missing_status_field_fails_closed(tmp_path: Path):
    _write_commitment(
        tmp_path,
        "2026-07-01-statusless-entry.yaml",
        "title: no status here\nmemo: cross-repo/archive/other-memo.md\n",
    )
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is False
    assert "no status field" in result.detail


def test_commitment_closure_matches_bare_filename_across_inbox_archive_sweep(tmp_path: Path):
    # Ledger entries cite memos by their DELIVERY path (cross-repo/inbox/...);
    _write_commitment(
        tmp_path,
        "2026-07-01-inbox-cited-entry.yaml",
        "title: cites delivery path\nstatus: open\n"
        "memo: cross-repo/inbox/2026-07-01-some-memo.md\n",
    )
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is False


def test_realized_by_path_shaped_exists(tmp_path: Path):
    target = tmp_path / "state" / "handoffs" / "some-handoff.md"
    target.parent.mkdir(parents=True)
    target.write_text("x\n", encoding="utf-8")
    assert resolve_realized_by("state/handoffs/some-handoff.md", tmp_path) is True


def test_realized_by_path_shaped_absent(tmp_path: Path):
    assert resolve_realized_by("state/handoffs/does-not-exist.md", tmp_path) is False


def test_realized_by_full_sha_real_object(git_repo: Path):
    sha = _commit_sha(git_repo)
    assert len(sha) == 40
    assert resolve_realized_by(sha, git_repo) is True


def test_realized_by_full_sha_fake_object(git_repo: Path):
    fake_sha = "f" * 40
    assert resolve_realized_by(fake_sha, git_repo) is False


def test_realized_by_bare_short_sha_real_object(git_repo: Path):
    sha = _commit_sha(git_repo)
    short = sha[:7]
    assert resolve_realized_by(short, git_repo) is True


def test_realized_by_bare_short_sha_fake_object(git_repo: Path):
    assert resolve_realized_by("b812d89", git_repo) is False


def test_realized_by_bare_short_sha_not_coerced_to_infinity(git_repo: Path):
    result = resolve_realized_by("717e385", git_repo)
    assert result in (True, False)
    assert result != float("inf")
    assert not isinstance(result, float)


def test_realized_by_inline_sentinel_resolves_true(tmp_path: Path):
    assert resolve_realized_by("inline", tmp_path) is True


def test_realized_by_absent_field_blocks(tmp_path: Path):
    result = check_realized_by("status: actioned\n", tmp_path)
    assert result.passed is False
    assert "absent" in result.detail


def test_git_objects_exist_empty_list_returns_empty_dict_no_spawn(tmp_path: Path, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must not spawn git for an empty sha list")

    monkeypatch.setattr(delete_guard.subprocess, "run", _fail_if_called)
    assert delete_guard._git_objects_exist([], tmp_path) == {}


def test_git_objects_exist_mixed_real_and_fake_full_shas(git_repo: Path):
    real = _commit_sha(git_repo)
    fake = "f" * 40
    result = delete_guard._git_objects_exist([real, fake], git_repo)
    assert result == {real: True, fake: False}


def test_git_objects_exist_mixed_real_and_fake_short_shas(git_repo: Path):
    real_short = _commit_sha(git_repo)[:7]
    fake_short = "b812d89"
    result = delete_guard._git_objects_exist([real_short, fake_short], git_repo)
    assert result == {real_short: True, fake_short: False}


def test_git_objects_exist_one_spawn_for_n_shas(git_repo: Path, monkeypatch):
    real = _commit_sha(git_repo)
    shas = [real, "f" * 40, "a" * 40, "b812d89"]
    calls = []
    orig_run = delete_guard.subprocess.run

    def _counting_run(*args, **kwargs):
        calls.append(args)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(delete_guard.subprocess, "run", _counting_run)
    result = delete_guard._git_objects_exist(shas, git_repo)
    assert len(calls) == 1
    assert result[real] is True
    assert result["f" * 40] is False
    assert result["a" * 40] is False
    assert result["b812d89"] is False


def test_git_objects_exist_duplicate_shas_collapse_to_one_entry(git_repo: Path):
    real = _commit_sha(git_repo)
    result = delete_guard._git_objects_exist([real, real], git_repo)
    assert result == {real: True}


def test_git_objects_exist_missing_reconciles_false_never_true(git_repo: Path):
    fake = "0" * 40
    result = delete_guard._git_objects_exist([fake], git_repo)
    assert result[fake] is False


def test_git_objects_exist_no_git_repo_fails_closed(tmp_path: Path):
    result = delete_guard._git_objects_exist(["f" * 40], tmp_path)
    assert result == {"f" * 40: False}


def test_check_realized_by_path_shaped_integration(tmp_path: Path):
    target = tmp_path / "docs" / "plans" / "some-plan.md"
    target.parent.mkdir(parents=True)
    target.write_text("x\n", encoding="utf-8")
    fm = "realized_by: docs/plans/some-plan.md\n"
    result = check_realized_by(fm, tmp_path)
    assert result.passed is True


def test_check_realized_by_unquotes_quoted_all_digit_sha(tmp_path: Path, monkeypatch):
    bare_sha = "44379324"
    quoted_on_disk = serialize_yaml_scalar(bare_sha, numeric_quoting=True)
    assert quoted_on_disk == f"'{bare_sha}'"

    seen = []

    def _fake_git_object_exists(sha: str, repo_root: Path) -> bool:
        seen.append(sha)
        return True

    monkeypatch.setattr(delete_guard, "_git_object_exists", _fake_git_object_exists)

    fm = f"realized_by: {quoted_on_disk}\n"
    result = check_realized_by(fm, tmp_path)

    assert result.passed is True
    assert seen == [bare_sha], "resolve_realized_by must see the unquoted bare SHA, not the raw quoted form"


def _commit_dated(repo_root: Path, filename: str, content: str, date: str) -> Path:
    """Commit `filename` with both author and committer date pinned to `date`
    (YYYY-MM-DD), so `_candidate_actioned_date` reads back exactly that day."""
    target = repo_root / filename
    target.write_text(content, encoding="utf-8")
    env_date = f"{date}T00:00:00"
    env = {
        "GIT_AUTHOR_DATE": env_date,
        "GIT_COMMITTER_DATE": env_date,
    }
    subprocess.run(["git", "add", filename], cwd=repo_root, check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "commit", "-q", "-m", f"add {filename}"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        env={**os.environ, **env},
        **no_console_creationflags(),
    )
    return target


def test_distill_fate_absent_predates_cutover_blocks_retain(git_repo: Path):
    # actioned date predates DISTILL_FATE_STAMPING_CUTOVER must RETAIN, never
    path = _commit_dated(git_repo, "pre-stamp-memo.md", "status: actioned\n", "2026-07-01")
    result = check_distill_fate("status: actioned\n", path, git_repo)
    assert result.passed is False
    assert "predates stamping cutover" in result.detail
    assert delete_guard.DISTILL_FATE_STAMPING_CUTOVER in result.detail


def test_distill_fate_absent_postcutover_passes(git_repo: Path):
    path = _commit_dated(git_repo, "post-stamp-memo.md", "status: actioned\n", "2026-08-01")
    result = check_distill_fate("status: actioned\n", path, git_repo)
    assert result.passed is True
    assert "on/after stamping cutover" in result.detail


def test_distill_fate_absent_undeterminable_blocks(tmp_path: Path):
    path = tmp_path / "uncommitted-memo.md"
    path.write_text("status: actioned\n", encoding="utf-8")
    result = check_distill_fate("status: actioned\n", path, tmp_path)
    assert result.passed is False
    assert "undeterminable" in result.detail


def test_distill_fate_absent_real_file_no_git_history_blocks_retain(git_repo: Path):
    # history for that exact path is the `_UNTRACKED` fast-path. This must
    path = git_repo / "never-committed-memo.md"
    path.write_text("status: actioned\n", encoding="utf-8")
    result = check_distill_fate("status: actioned\n", path, git_repo)
    assert result.passed is False
    assert "no git history" in result.detail
    assert delete_guard.DISTILL_FATE_STAMPING_CUTOVER in result.detail


def test_distill_fate_ephemeral_passes(tmp_path: Path):
    result = check_distill_fate("distill_fate: ephemeral\n", tmp_path / "x.md", tmp_path)
    assert result.passed is True


def test_distill_fate_commitment_passes(tmp_path: Path):
    result = check_distill_fate("distill_fate: commitment\n", tmp_path / "x.md", tmp_path)
    assert result.passed is True


def test_distill_fate_ratification_valid_capture_passes(tmp_path: Path):
    target = tmp_path / "docs" / "wiki" / "captured.md"
    target.parent.mkdir(parents=True)
    target.write_text("captured content\n", encoding="utf-8")
    fm = "distill_fate: ratification\nin_repo_capture: docs/wiki/captured.md\n"
    result = check_distill_fate(fm, tmp_path / "x.md", tmp_path)
    assert result.passed is True


def test_distill_fate_ratification_absent_capture_blocks(tmp_path: Path):
    result = check_distill_fate("distill_fate: ratification\n", tmp_path / "x.md", tmp_path)
    assert result.passed is False
    assert "in_repo_capture" in result.detail
    assert "absent" in result.detail


def test_distill_fate_ratification_unresolved_capture_blocks(tmp_path: Path):
    fm = "distill_fate: ratification\nin_repo_capture: docs/wiki/does-not-exist.md\n"
    result = check_distill_fate(fm, tmp_path / "x.md", tmp_path)
    assert result.passed is False
    assert "does not resolve on disk" in result.detail


def test_distill_fate_ratification_empty_capture_blocks(tmp_path: Path):
    target = tmp_path / "docs" / "wiki" / "empty-capture.md"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    fm = "distill_fate: ratification\nin_repo_capture: docs/wiki/empty-capture.md\n"
    result = check_distill_fate(fm, tmp_path / "x.md", tmp_path)
    assert result.passed is False
    assert "empty" in result.detail


def test_distill_fate_unrecognized_value_blocks(tmp_path: Path):
    result = check_distill_fate("distill_fate: bogus-value\n", tmp_path / "x.md", tmp_path)
    assert result.passed is False
    assert "unrecognized distill_fate" in result.detail


@_requires_rg
def test_distill_fate_flows_into_receipts_blocked_by(tmp_path: Path):
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "ratified-memo.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: claude-klabauter-em\n"
        "status: actioned\n"
        "realized_by: inline\n"
        "distill_fate: ratification\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    receipt = evaluate_candidate_receipts(memo, tmp_path, ())
    assert receipt["eligible"] is False
    assert "distill-fate" in receipt["blocked_by"]
    guard_names = {g["guard"] for g in receipt["guards_run"]}
    assert "distill-fate" in guard_names
    fate_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "distill-fate")
    assert fate_receipt["verdict"] == "block"


@_requires_rg
def test_commitment_inline_no_docs_citation_blocks_via_harvest_provenance(tmp_path: Path):
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "committed-memo.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: claude-klabauter-em\n"
        "status: actioned\n"
        "realized_by: inline\n"
        "distill_fate: commitment\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    receipt = evaluate_candidate_receipts(memo, tmp_path, ())
    assert receipt["eligible"] is False
    assert "harvest-provenance" in receipt["blocked_by"]
    guard_names = {g["guard"] for g in receipt["guards_run"]}
    assert "harvest-provenance" in guard_names
    harvest_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "harvest-provenance")
    assert harvest_receipt["verdict"] == "block"
    fate_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "distill-fate")
    assert fate_receipt["verdict"] == "pass"
    realized_by_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "realized_by")
    assert realized_by_receipt["verdict"] == "pass"


@_requires_rg
def test_commitment_inline_with_docs_citation_passes(tmp_path: Path):
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "committed-memo-cited.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: claude-klabauter-em\n"
        "status: actioned\n"
        "realized_by: inline\n"
        "distill_fate: commitment\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    wiki_dir = tmp_path / "docs" / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    # Cite the BASENAME only, not the full repo-relative path: harvest-provenance
    (wiki_dir / "landing-notes.md").write_text(
        "Landed per committed-memo-cited.md\n",
        encoding="utf-8",
    )
    receipt = evaluate_candidate_receipts(memo, tmp_path, ())
    assert receipt["eligible"] is True
    assert receipt["blocked_by"] == []
    harvest_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "harvest-provenance")
    assert harvest_receipt["verdict"] == "pass"


def _full_handoff_candidate(tmp_path: Path, basis_refs: tuple[str, ...]) -> DeleteCandidate:
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    handoff = tmp_path / "state" / "handoffs" / "candidate.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "---\n"
        "shipped_in: 68b27420\n"
        "status: open\n"
        "deployment_state: shipped\n"
        "realized_by: inline\n"
        "distill_fate: ephemeral\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    return DeleteCandidate(path=handoff, repo_root=tmp_path, basis_refs=basis_refs)


def _full_memo_candidate(tmp_path: Path, basis_refs: tuple[str, ...]) -> DeleteCandidate:
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "memo-candidate.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: claude-klabauter-em\n"
        "status: actioned\n"
        "realized_by: inline\n"
        "distill_fate: ephemeral\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    return DeleteCandidate(path=memo, repo_root=tmp_path, basis_refs=basis_refs)


@_requires_rg
def test_memory_pointer_exclusion_only_claude_path_forces_retain(tmp_path: Path):
    candidate = _full_handoff_candidate(tmp_path, ("~/.claude/state/lessons/foo.md",))
    outcome = evaluate_candidate(candidate)
    assert outcome["eligible"] is False
    assert "memory-pointer-exclusion" in outcome["blocked_by"]


@_requires_rg
def test_memory_pointer_exclusion_mixed_basis_does_not_fire(tmp_path: Path):
    candidate = _full_handoff_candidate(
        tmp_path,
        ("~/.claude/state/lessons/foo.md", "docs/decisions/2026-07-01-some-dr.md"),
    )
    outcome = evaluate_candidate(candidate)
    assert "memory-pointer-exclusion" not in outcome["blocked_by"]


@_requires_rg
def test_memory_pointer_exclusion_no_basis_refs_does_not_fire(tmp_path: Path):
    candidate = _full_handoff_candidate(tmp_path, ())
    outcome = evaluate_candidate(candidate)
    assert "memory-pointer-exclusion" not in outcome["blocked_by"]


def test_classify_artifact_frontmatter_shape(tmp_path: Path):
    anywhere = tmp_path / "unplaced.md"
    assert (
        delete_guard.classify_artifact("from: a-em\nto: b-em\n", anywhere, tmp_path)
        == "memo"
    )
    assert (
        delete_guard.classify_artifact("deployment_state: shipped\n", anywhere, tmp_path)
        == "handoff"
    )


def test_classify_artifact_path_prefix_fallback(tmp_path: Path):
    memo_path = tmp_path / "cross-repo" / "archive" / "x.md"
    handoff_path = tmp_path / "archive" / "handoffs" / "2026-07" / "x.md"
    assert delete_guard.classify_artifact("", memo_path, tmp_path) == "memo"
    assert delete_guard.classify_artifact("", handoff_path, tmp_path) == "handoff"


def test_classify_artifact_path_prefix_fallback_migrated_root(tmp_path: Path):
    memo_path = tmp_path / "state" / "cross-repo" / "archive" / "x.md"
    assert delete_guard.classify_artifact("", memo_path, tmp_path) == "memo"


def test_classify_artifact_unresolvable_returns_none(tmp_path: Path):
    assert (
        delete_guard.classify_artifact("status: open\n", tmp_path / "misc.md", tmp_path)
        is None
    )


@_requires_rg
def test_memo_not_evaluated_by_shipped_in(tmp_path: Path):
    candidate = _full_memo_candidate(tmp_path, ())
    outcome = evaluate_candidate(candidate)
    assert outcome["artifact_class"] == "memo"
    assert "shipped_in" not in outcome["blocked_by"]
    assert outcome["eligible"] is True


@_requires_rg
def test_handoff_not_evaluated_by_status_actioned(tmp_path: Path):
    candidate = _full_handoff_candidate(tmp_path, ())
    outcome = evaluate_candidate(candidate)
    assert outcome["artifact_class"] == "handoff"
    assert "status-actioned" not in outcome["blocked_by"]
    assert outcome["eligible"] is True


@_requires_rg
def test_unclassifiable_candidate_fails_closed(tmp_path: Path):
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True)
    unplaced = tmp_path / "misc-candidate.md"
    unplaced.write_text(
        "---\nstatus: open\nrealized_by: inline\n---\nbody\n", encoding="utf-8"
    )
    candidate = DeleteCandidate(path=unplaced, repo_root=tmp_path, basis_refs=())
    outcome = evaluate_candidate(candidate)
    assert outcome["eligible"] is False
    assert outcome["artifact_class"] is None
    assert "artifact-class-unresolved" in outcome["blocked_by"]


@_requires_rg
def test_evaluate_candidate_fully_eligible(tmp_path: Path):
    candidate = _full_handoff_candidate(tmp_path, ())
    outcome = evaluate_candidate(candidate)
    assert outcome["eligible"] is True
    assert outcome["blocked_by"] == []


@_requires_rg
def test_evaluate_candidate_open_commitment_blocks(tmp_path: Path):
    candidate = _full_handoff_candidate(tmp_path, ())
    _write_commitment(
        tmp_path,
        "2026-07-01-open-entry.yaml",
        "title: pending\nstatus: open\nmemo: state/handoffs/candidate.md\n",
    )
    outcome = evaluate_candidate(candidate)
    assert outcome["eligible"] is False
    assert "commitment-closure" in outcome["blocked_by"]


@_requires_rg
def test_evaluate_candidate_uses_repo_relative_needle_not_bare_filename(tmp_path: Path):
    candidate_dir = tmp_path / "cross-repo" / "archive"
    candidate_dir.mkdir(parents=True)
    handoff = candidate_dir / "candidate.md"
    handoff.write_text(
        "---\nshipped_in: 68b27420\nstatus: actioned\nrealized_by: inline\n---\nbody\n",
        encoding="utf-8",
    )

    # A docs/ note references a DIFFERENT file that happens to share the bare
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(
        "see some/other/unrelated/candidate.md for context\n", encoding="utf-8"
    )

    candidate = DeleteCandidate(path=handoff, repo_root=tmp_path, basis_refs=())
    outcome = evaluate_candidate(candidate)
    assert "active-reference" not in outcome["blocked_by"]


@_requires_rg
def test_evaluate_candidate_needle_is_forward_slash_not_native_separator(tmp_path: Path):
    # SECURITY-ADJACENT regression, companion to the repo-relative needle test above.
    candidate_dir = tmp_path / "cross-repo" / "archive"
    candidate_dir.mkdir(parents=True)
    handoff = candidate_dir / "candidate.md"
    handoff.write_text(
        "---\nshipped_in: 68b27420\nstatus: actioned\nrealized_by: inline\n---\nbody\n",
        encoding="utf-8",
    )

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(
        "still depends on cross-repo/archive/candidate.md\n", encoding="utf-8"
    )

    candidate = DeleteCandidate(path=handoff, repo_root=tmp_path, basis_refs=())
    outcome = evaluate_candidate(candidate)
    assert set(outcome["blocked_by"]) == {
        "active-reference",
        "commitment-closure",
        "distill-fate",
    }


def test_evaluate_candidate_needle_shape_is_posix_on_every_platform(monkeypatch, tmp_path: Path):
    candidate_dir = tmp_path / "cross-repo" / "archive"
    candidate_dir.mkdir(parents=True)
    handoff = candidate_dir / "candidate.md"
    handoff.write_text(
        "---\nshipped_in: 68b27420\nstatus: actioned\nrealized_by: inline\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()

    seen: list[str] = []

    def _capture(needle, repo_root, **kwargs):
        seen.append(needle)
        return False

    monkeypatch.setattr(delete_guard, "active_reference_guard", _capture)
    candidate = DeleteCandidate(path=handoff, repo_root=tmp_path, basis_refs=())
    evaluate_candidate(candidate)

    assert "cross-repo/archive/candidate.md" in seen
    assert not any("\\" in needle for needle in seen)


# is deletable. Guard 7 (harvest-provenance) REQUIRES it: a tombstone is proof the content

@_requires_rg
def test_guard7_counts_provenance_block_as_durable_capture_proof(tmp_path: Path):
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "guide.md").write_text(
        "---\n"
        "archived_handoff:\n"
        "  - path: archive/handoffs/old-thing.md\n"
        "    workstream: foo\n"
        "---\n"
        "the harvested content lives here now\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "archive" / "handoffs" / "old-thing.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("body\n", encoding="utf-8")

    result = check_harvest_provenance("distill_fate: commitment", candidate, tmp_path)
    assert result.passed is True


@_requires_rg
def test_guard3_excludes_the_same_block_guard7_requires(tmp_path: Path):
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "guide.md").write_text(
        "---\n"
        "archived_handoff:\n"
        "  - path: archive/handoffs/old-thing.md\n"
        "    workstream: foo\n"
        "---\n"
        "the harvested content lives here now\n",
        encoding="utf-8",
    )
    assert active_reference_guard("archive/handoffs/old-thing.md", tmp_path) is False


@_requires_rg
def test_guard7_basename_collision_in_unrelated_provenance_block_does_not_pass(
    tmp_path: Path,
):
    # provenance block belonging to a DIFFERENT harvested artifact that merely shares this
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "guide.md").write_text(
        "---\n"
        "archived_handoff:\n"
        "  - path: some/other/dir/old-thing.md\n"
        "    workstream: foo\n"
        "---\n"
        "unrelated content\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "archive" / "handoffs" / "old-thing.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("body\n", encoding="utf-8")

    result = check_harvest_provenance("distill_fate: commitment", candidate, tmp_path)
    assert result.passed is False


@_requires_rg
def test_guard7_basename_citation_in_prose_still_passes(tmp_path: Path):
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "guide.md").write_text(
        "the harvested content for old-thing.md lives here now\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "archive" / "handoffs" / "old-thing.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("body\n", encoding="utf-8")

    result = check_harvest_provenance("distill_fate: commitment", candidate, tmp_path)
    assert result.passed is True


@_requires_rg
def test_evaluate_candidate_multiple_guards_fail(tmp_path: Path):
    handoff = tmp_path / "state" / "handoffs" / "bad-candidate.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text(
        "---\n"
        "status: active\n"
        "deployment_state: awaiting_gate\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    candidate = DeleteCandidate(path=handoff, repo_root=tmp_path, basis_refs=())
    outcome = evaluate_candidate(candidate)
    assert outcome["eligible"] is False
    assert "shipped_in" in outcome["blocked_by"]
    assert "realized_by" in outcome["blocked_by"]
    assert "commitment-closure" in outcome["blocked_by"]
    assert "status-actioned" not in outcome["blocked_by"]


@_requires_rg
def test_evaluate_candidate_detailed_matches_evaluate_candidate(tmp_path: Path):
    candidate = _full_handoff_candidate(tmp_path, ())
    outcome = evaluate_candidate(candidate)
    artifact_class, guard_results = evaluate_candidate_detailed(candidate.path, candidate.repo_root)

    assert artifact_class == outcome["artifact_class"]
    assert {r.guard for r in guard_results if not r.passed} == set(outcome["blocked_by"])
    assert outcome["eligible"] == (len(guard_results) > 0 and all(r.passed for r in guard_results))


@_requires_rg
def test_evaluate_candidate_detailed_blocked_matches(tmp_path: Path):
    handoff = tmp_path / "state" / "handoffs" / "bad-candidate.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text(
        "---\nstatus: active\ndeployment_state: awaiting_gate\n---\nbody\n",
        encoding="utf-8",
    )
    candidate = DeleteCandidate(path=handoff, repo_root=tmp_path, basis_refs=())
    outcome = evaluate_candidate(candidate)
    artifact_class, guard_results = evaluate_candidate_detailed(candidate.path, candidate.repo_root)

    assert artifact_class == "handoff" == outcome["artifact_class"]
    blocked_from_detailed = {r.guard for r in guard_results if not r.passed}
    assert blocked_from_detailed == set(outcome["blocked_by"])
    assert "shipped_in" in blocked_from_detailed
    assert "realized_by" in blocked_from_detailed
