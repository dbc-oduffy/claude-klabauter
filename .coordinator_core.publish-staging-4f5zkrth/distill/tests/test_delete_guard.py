"""
coordinator_core.distill.tests.test_delete_guard

Unit tests for coordinator_core.distill.delete_guard — the mechanical delete-safety
guard for handoffs and cross-repo memos.

Coverage:
  check_shipped_in:
    (a) present + non-empty -> passes
    (b) absent -> blocks
  check_status_actioned:
    (c) status: actioned -> passes
    (d) status: active -> blocks
  check_active_reference:
    (e) needle referenced under docs/ -> blocks (still-referenced)
    (f) needle not referenced anywhere -> passes
  check_commitment_closure (real closure check over the ledger's `status:` field —
  replaced the former always-block "closure schema not yet defined" placeholder,
  which hard-blocked every candidate the moment the surface existed):
    (g) FAIL LOUD: state/cross-repo-commitments absent -> blocks with the exact
        "commitment-closure: surface absent" detail (never a silent pass)
    (g2) OPEN commitment referencing the candidate -> blocks
    (g3) CLOSED commitment referencing the candidate -> passes
    (g4) open commitment NOT referencing the candidate -> passes
    (g5) unparseable ledger entry -> fail-closed block with a reason naming the entry
    (g6) ledger entry with no status field -> fail-closed block
    (g7) open commitment citing the candidate's delivery path (cross-repo/inbox/)
        while the candidate lives in cross-repo/archive/ -> still blocks (bare
        filename matching bridges the inbox->archive sweep)
  resolve_realized_by / check_realized_by (per-value-shape dispatch, one fixture row
  per shape — this is the reviewer-flagged high-risk section):
    (h) path-shaped, file exists -> True
    (i) path-shaped, file absent -> False
    (j) full 40-char SHA, real git object -> True (via git cat-file -e)
    (k) full 40-char SHA, not a real object -> False
    (l) bare-short SHA (e.g. b812d89 / b6143a5 shape), real git object -> True —
        MUST NOT be coerced to Infinity (realized-by-short-sha-scientific-notation-trap)
    (m) bare-short SHA, not a real object -> False
    (n) literal "inline" sentinel -> True unconditionally
    (o) absent realized_by field entirely -> blocks with a distinct detail message
  #12 memory-pointer exclusion:
    (p) basis_refs citing ONLY a ~/.claude path -> forced RETAIN (ineligible),
        "memory-pointer-exclusion" present in blocked_by regardless of the other
        5 guards' outcome
    (q) basis_refs citing a ~/.claude path PLUS another non-memory reference ->
        exclusion does NOT fire (mixed basis is not "ONLY")
    (r) no basis_refs at all -> exclusion does not fire
  classify_artifact + class-keyed guard dispatch (the 2026-07-23 opticon
  164/164-blocked defect: shipped_in ran against every memo and status-actioned
  against every handoff — class-inapplicable guards must not run at all):
    (u) from:+to: frontmatter -> memo; deployment_state: -> handoff; path-prefix
        fallback (cross-repo/ -> memo, handoffs segment -> handoff); neither ->
        None
    (v) a memo is never evaluated by shipped_in; a handoff never by
        status-actioned
    (w) an unclassifiable candidate fails CLOSED with "artifact-class-unresolved"
  evaluate_candidate (integration):
    (s) a candidate that clears every applicable guard -> eligible True,
        blocked_by empty
    (t) a candidate that fails several guards -> eligible False, blocked_by lists
        every failing guard name

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C3
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.distill import delete_guard
from coordinator_core.frontmatter.primitives import serialize_yaml_scalar
from coordinator_core.distill.delete_guard import (
    DeleteCandidate,
    check_active_reference,
    check_commitment_closure,
    check_distill_fate,
    check_realized_by,
    check_shipped_in,
    check_status_actioned,
    evaluate_candidate,
    evaluate_candidate_detailed,
    resolve_realized_by,
)
from coordinator_core.ops.distill_disposal_manifest import evaluate_candidate_receipts

_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")

# Declared, not excused: the `git_repo` fixture and `_commit_dated` spawn real git
# because the properties under test are real git object resolution --
# `resolve_realized_by`/`_git_objects_exist` dispatch through `git cat-file` against
# real full/short SHAs (the scientific-notation-coercion hazard needs a real
# resolvable object, not a mock), and `check_distill_fate`'s absent-fate branch reads
# real `git log`-derived commit dates to compare against DISTILL_FATE_STAMPING_CUTOVER.
# `git_repo` stays function-scoped (default fixture scope) because
# `test_distill_fate_absent_real_file_no_git_history_blocks_retain` and its siblings
# add distinct uncommitted/differently-dated files per test that must not leak between
# tests sharing a repo. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one committed file, giving us a real, resolvable
    full-length SHA and a real, resolvable bare-short-SHA prefix to test against."""
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


# ---------------------------------------------------------------------------
# check_shipped_in
# ---------------------------------------------------------------------------

def test_shipped_in_present_passes():
    result = check_shipped_in("shipped_in: 68b27420\nstatus: actioned\n")
    assert result.passed is True


def test_shipped_in_absent_blocks():
    result = check_shipped_in("status: actioned\n")
    assert result.passed is False
    assert "absent" in result.detail


# ---------------------------------------------------------------------------
# check_status_actioned
# ---------------------------------------------------------------------------

def test_status_actioned_passes():
    result = check_status_actioned("status: actioned\n")
    assert result.passed is True


def test_status_active_blocks():
    result = check_status_actioned("status: active\n")
    assert result.passed is False
    assert "active" in result.detail


# ---------------------------------------------------------------------------
# check_active_reference
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# check_commitment_closure — real closure check over the ledger's status field
# ---------------------------------------------------------------------------

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
    # Explicit negative-spec assertion: absence must never resolve to passed=True.
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is not True


def test_commitment_closure_empty_surface_passes(tmp_path: Path):
    # An existing ledger dir with no entries has no open commitments — the
    # former "closure schema not yet defined" placeholder hard-blocked here
    # (the 2026-07-23 opticon defect, part a); the real check must not.
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
    # an actioned candidate has been swept to cross-repo/archive/ under the same
    # filename. The filename leg of reference detection must bridge that.
    _write_commitment(
        tmp_path,
        "2026-07-01-inbox-cited-entry.yaml",
        "title: cites delivery path\nstatus: open\n"
        "memo: cross-repo/inbox/2026-07-01-some-memo.md\n",
    )
    result = check_commitment_closure(_NEEDLE, tmp_path)
    assert result.passed is False


# ---------------------------------------------------------------------------
# resolve_realized_by — one fixture row per value SHAPE
# ---------------------------------------------------------------------------

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
    # Sanity: this is exactly the shape (hex-only, 7 chars) that a naive
    # numeric-coercion path would misread as scientific notation.
    assert resolve_realized_by(short, git_repo) is True


def test_realized_by_bare_short_sha_fake_object(git_repo: Path):
    # b812d89-shaped (7 hex chars) but not a real object in this throwaway repo.
    assert resolve_realized_by("b812d89", git_repo) is False


def test_realized_by_bare_short_sha_not_coerced_to_infinity(git_repo: Path):
    # The exact hazard named in the auto-memory lesson: a bare short SHA like
    # 717e385 must resolve via git, never via float()/int() coercion (which
    # would silently produce Infinity instead of raising or resolving False).
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


# ---------------------------------------------------------------------------
# _git_objects_exist (batched sibling of _git_object_exists)
# ---------------------------------------------------------------------------

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
    # Reconciliation regression: a `<sha> missing` batch-check record must
    # never be misread as "exists" — the delete-guard's failure direction is
    # asymmetric (a false "exists" permits a deletion it should have blocked).
    fake = "0" * 40
    result = delete_guard._git_objects_exist([fake], git_repo)
    assert result[fake] is False


def test_git_objects_exist_no_git_repo_fails_closed(tmp_path: Path):
    # tmp_path is not a git repo at all — cat-file itself fails; every
    # requested sha must resolve to False, never crash, never True.
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
    # Regression: memo_transition.py writes realized_by with numeric_quoting=True,
    # so an all-digit short SHA lands on disk as `realized_by: '44379324'` and
    # reads back WITH quotes. check_realized_by must unquote before dispatching
    # to resolve_realized_by's SHA regex — otherwise the quoted value falls
    # through to the path-shaped fallback (always False, since "'44379324'" is
    # never a real path) and a legitimately-resolvable realized_by falsely
    # blocks an eligible delete.
    bare_sha = "44379324"
    quoted_on_disk = serialize_yaml_scalar(bare_sha, numeric_quoting=True)
    assert quoted_on_disk == f"'{bare_sha}'"  # precondition: on-disk shape is quoted

    seen = []

    def _fake_git_object_exists(sha: str, repo_root: Path) -> bool:
        seen.append(sha)
        return True

    monkeypatch.setattr(delete_guard, "_git_object_exists", _fake_git_object_exists)

    fm = f"realized_by: {quoted_on_disk}\n"
    result = check_realized_by(fm, tmp_path)

    assert result.passed is True
    assert seen == [bare_sha], "resolve_realized_by must see the unquoted bare SHA, not the raw quoted form"


# ---------------------------------------------------------------------------
# check_distill_fate — Guard 6 (2026-07-23 distill-delete-guard-fate-enforcement E1)
# ---------------------------------------------------------------------------

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
    subprocess.run(["git", "add", filename], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"add {filename}"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        env={**os.environ, **env},
    )
    return target


def test_distill_fate_absent_predates_cutover_blocks_retain(git_repo: Path):
    # Ruling (b), the safety-floor fix: an absent-fate memo whose git-history
    # actioned date predates DISTILL_FATE_STAMPING_CUTOVER must RETAIN, never
    # be silently delete-eligible.
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
    # A non-repo path (git log fails) is undeterminable -> fail-closed retain,
    # never treated as "must be recent".
    path = tmp_path / "uncommitted-memo.md"
    path.write_text("status: actioned\n", encoding="utf-8")
    result = check_distill_fate("status: actioned\n", path, tmp_path)
    assert result.passed is False
    assert "undeterminable" in result.detail


def test_distill_fate_absent_real_file_no_git_history_blocks_retain(git_repo: Path):
    # Review: code-reviewer Finding 1 (2026-08-06) — a real, on-disk file inside a
    # WORKING git repo (git log succeeds, returncode 0) but with ZERO commit
    # history for that exact path is the `_UNTRACKED` fast-path. This must
    # fail-closed (retain), not PASS: a shallow clone, a `git gc` after a
    # rebase/squash, or a sparse/filtered checkout can each produce this exact
    # signature for a genuinely old, fully-committed memo, indistinguishable
    # from "never committed" to `_candidate_actioned_date`. No prior fixture
    # exercised this — every `git_repo` fixture use committed the file first.
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
    # Commitment defers its durable-capture obligation to Guard 5
    # (realized_by) — this guard alone does not independently block it.
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
    # A ratification candidate missing its durable capture must both block
    # eligibility AND surface in evaluate_candidate_receipts's blocked_by/
    # guards_run — the manifest-assembly consumer of the same dispatch order.
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "ratified-memo.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: project-makima-em\n"
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


# ---------------------------------------------------------------------------
# check_harvest_provenance — Guard 7 (2026-07-23 code-review Finding 1: the
# Gap 2 fold into realized_by alone was insufficient — see delete_guard.py's
# module docstring guard-6/7 entries)
# ---------------------------------------------------------------------------

@_requires_rg
def test_commitment_inline_no_docs_citation_blocks_via_harvest_provenance(tmp_path: Path):
    # This is the exact gap Finding 1 named: distill_fate=commitment +
    # realized_by=inline sails through check_distill_fate (defers) and
    # check_realized_by (inline resolves True unconditionally) with zero
    # verification the content ever reached docs/wiki or docs/decisions.
    # check_harvest_provenance must independently block it.
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "committed-memo.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: project-makima-em\n"
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
    # The two guards it was formerly deferred to must both still PASS —
    # harvest-provenance is the ONLY thing blocking this candidate.
    fate_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "distill-fate")
    assert fate_receipt["verdict"] == "pass"
    realized_by_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "realized_by")
    assert realized_by_receipt["verdict"] == "pass"


@_requires_rg
def test_commitment_inline_with_docs_citation_passes(tmp_path: Path):
    # Positive case: the same commitment/inline shape, but the candidate's
    # content DID make it into docs/wiki — harvest-provenance must find the
    # citation and pass, alongside every other applicable guard.
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "committed-memo-cited.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: project-makima-em\n"
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
    # matches needle-OR-basename, but a full-path citation here would also trip
    # check_active_reference (same needle, same docs/ scope) as "still
    # referenced" and block the candidate for an unrelated reason — this test
    # isolates harvest-provenance's own pass condition.
    (wiki_dir / "landing-notes.md").write_text(
        "Landed per committed-memo-cited.md\n",
        encoding="utf-8",
    )
    receipt = evaluate_candidate_receipts(memo, tmp_path, ())
    assert receipt["eligible"] is True
    assert receipt["blocked_by"] == []
    harvest_receipt = next(g for g in receipt["guards_run"] if g["guard"] == "harvest-provenance")
    assert harvest_receipt["verdict"] == "pass"


# ---------------------------------------------------------------------------
# #12 memory-pointer exclusion
# ---------------------------------------------------------------------------

def _full_handoff_candidate(tmp_path: Path, basis_refs: tuple[str, ...]) -> DeleteCandidate:
    """A handoff-shaped candidate that clears every handoff-applicable guard
    (empty commitments ledger included, so commitment-closure has a real pass)."""
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
    """A memo-shaped candidate that clears every memo-applicable guard."""
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    memo = tmp_path / "cross-repo" / "archive" / "memo-candidate.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(
        "---\n"
        "from: sibling-em\n"
        "to: project-makima-em\n"
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


# ---------------------------------------------------------------------------
# classify_artifact + class-keyed guard dispatch
# ---------------------------------------------------------------------------

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


def test_classify_artifact_unresolvable_returns_none(tmp_path: Path):
    assert (
        delete_guard.classify_artifact("status: open\n", tmp_path / "misc.md", tmp_path)
        is None
    )


@_requires_rg
def test_memo_not_evaluated_by_shipped_in(tmp_path: Path):
    # The 2026-07-23 opticon defect (part b): memos never carry shipped_in, so
    # the handoff-only guard blocked every memo. Class-keyed dispatch must not
    # run it against a memo at all.
    candidate = _full_memo_candidate(tmp_path, ())
    outcome = evaluate_candidate(candidate)
    assert outcome["artifact_class"] == "memo"
    assert "shipped_in" not in outcome["blocked_by"]
    assert outcome["eligible"] is True


@_requires_rg
def test_handoff_not_evaluated_by_status_actioned(tmp_path: Path):
    # Mirror half of the same defect: a live handoff's status vocabulary is
    # open/closed, never "actioned" — the memo-only guard must not run.
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


# ---------------------------------------------------------------------------
# evaluate_candidate — integration
# ---------------------------------------------------------------------------

@_requires_rg
def test_evaluate_candidate_fully_eligible(tmp_path: Path):
    # Formerly pinned as unreachable (the placeholder commitment-closure guard
    # blocked every candidate the moment the surface existed); with the real
    # closure check, a clean candidate over an open-commitment-free ledger is
    # eligible.
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
    # Review: workflow-review (2026-07-12) — evaluate_candidate previously passed
    # candidate.path.name (bare filename) to check_active_reference, a strictly
    # looser rg needle than sidecar_sweep's sibling caller of the same shared
    # guard (which passes the repo-relative path). This test pins the fix: a
    # reference doc that mentions the SAME bare filename under an unrelated
    # subdirectory must NOT trip the active-reference guard for a candidate
    # living at a different path — only a hit on the repo-relative path blocks.
    candidate_dir = tmp_path / "cross-repo" / "archive"
    candidate_dir.mkdir(parents=True)
    handoff = candidate_dir / "candidate.md"
    handoff.write_text(
        "---\nshipped_in: 68b27420\nstatus: actioned\nrealized_by: inline\n---\nbody\n",
        encoding="utf-8",
    )

    # A docs/ note references a DIFFERENT file that happens to share the bare
    # filename "candidate.md" in an unrelated directory — a bare-filename needle
    # would false-positive-match this; a repo-relative needle correctly does not.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(
        "see some/other/unrelated/candidate.md for context\n", encoding="utf-8"
    )

    candidate = DeleteCandidate(path=handoff, repo_root=tmp_path, basis_refs=())
    outcome = evaluate_candidate(candidate)
    assert "active-reference" not in outcome["blocked_by"]


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
    # commitment-closure blocks here via the absent-surface fail-loud branch.
    assert "commitment-closure" in outcome["blocked_by"]
    # Memo-only guard must NOT run against a handoff-classified candidate.
    assert "status-actioned" not in outcome["blocked_by"]


# ---------------------------------------------------------------------------
# evaluate_candidate_detailed — single dispatch-order authority (§ 2a)
# ---------------------------------------------------------------------------


@_requires_rg
def test_evaluate_candidate_detailed_matches_evaluate_candidate(tmp_path: Path):
    """evaluate_candidate is now a thin wrapper over evaluate_candidate_detailed
    (2026-07-23 architecture review § 2a) — pin that the two stay in exact
    agreement: same artifact_class, same set of guards that ran, same set of
    guards that blocked."""
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
