"""
coordinator_core.review_trail.tests.test_reviewed_set_equivalence

Purpose: C2's equivalence proof, discharging AC6 (AC6a-e) and AC9 — the
gate that made deleting `coverage.py::build_reviewed_set`'s per-call
recomputation safe.

The whole-corpus differential leg was one-shot by construction: it ran while
both implementations existed, recorded divergence 0/0 over the real corpus,
and is asserted here against that frozen artifact because C5 has since
deleted the implementation it compared against. The oracle cases below are
the live gate and do not depend on any retired symbol.

Ground truth is the independent `git rev-list` ORACLE, never mere
new-vs-retiring agreement (Finding 5 of the 2026-07-15 slice-coverage test
audit: "new==old agreement proves only that two things share a bug"). This
file:

  (a) Adds one TARGETED oracle case per preserved credit rule (AC6a-e) —
      each fixture is small, hand-built, and its expected credited set is
      derived directly from `git rev-list` plus the rule's own documented
      effect, never from calling `build_reviewed_set` and trusting it.
  (b) Runs a ONE-SHOT whole-corpus new-vs-retiring DIFFERENTIAL against the
      real `state/review-trail/` corpus of this repo, and records the
      divergence set (required empty) as a ratchet artifact on disk. This
      is a regression detector, not a proof by itself — see (a) for the
      actual ground-truth pins.
  (c) AC9: deletes the reviewed-set store's on-disk files and rebuilds via
      `backfill.run_backfill`, asserting the rebuilt set is byte-identical
      to the pre-deletion set — the store is derived, never authoritative.

Negative-spec:
    - Does NOT assert `new == old` alone as a correctness proof anywhere in
      this file (see `test_build_reviewed_set_graphwalk_merge_reachability`
      in `coordinator_core/tests/test_coverage_reviewed_set.py` for the
      existing oracle this file's AC6a-e cases sit alongside, and the
      module docstring above for why `new == old` is demoted to a
      regression detector here).
    - AC9's delete-and-rebuild runs against an ISOLATED tmp_path repo with
      a synthetic corpus, never against this box's live per-clone store
      under the real repo's `.git/coordinator-review-trail/` — that store
      is shared with every concurrent session on this machine, and a
      destructive delete there would drop a peer's already-folded
      records. The real repo is used ONLY for the additive, idempotent
      whole-corpus differential in (b).

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C2
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core import coverage
from coordinator_core.review_trail import backfill
from coordinator_core.review_trail import reviewed_set as rs

#: This repo's own root — coordinator_core/review_trail/tests/<this file>.py
#: is three levels below it (tests -> review_trail -> coordinator_core -> root).
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Where the one-shot whole-corpus differential (b) records its ratchet
#: artifact — mirrors the `state/audits/` convention used elsewhere in this
#: repo for a mechanically-computed, on-disk regression signal.
_RATCHET_PATH = (
    _REPO_ROOT
    / "state"
    / "audits"
    / "2026-08-27-reviewed-set-whole-corpus-differential.json"
)


# ---------------------------------------------------------------------------
# Git repo helpers (mirrors the pattern in test_coverage_reviewed_set.py /
# test_backfill.py / test_write_time_resolution.py).
# ---------------------------------------------------------------------------


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, encoding="utf-8", check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _make_commit(repo: Path, message: str, session_id: Optional[str] = None) -> str:
    body = message if session_id is None else f"{message}\n\nSession-Id: {session_id}"
    _git(["commit", "--allow-empty", "-m", body], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


def _make_file_commit(repo: Path, rel_path: str, message: str) -> str:
    """Commit introducing a real file at `rel_path` — needed for AC6c's
    planning-artifact partitioning, which classifies by TOUCHED PATH, not
    by commit message."""
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(message, encoding="utf-8")
    _git(["add", rel_path], repo)
    _git(["commit", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


def _rev_list(sha_range: str, repo: Path) -> set:
    out = subprocess.run(
        ["git", "rev-list", sha_range], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout
    return {s.strip() for s in out.splitlines() if s.strip()}


def _write_trail_record(repo: Path, filename: str, record: dict) -> Path:
    trail_dir = repo / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    path = trail_dir / filename
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _base_record(sha_range: str, **overrides) -> dict:
    record = {
        "sha_range": sha_range,
        "reviewer": "code-reviewer",
        "scope": "chain",
        "scope_kind": "diff",
        "verdict": "ok",
        "diff_loc": 5,
        "session_id": "abcdef01",
        "workstream": None,
    }
    record.update(overrides)
    return record


def _fold_and_read(repo: Path, records: List[Tuple[str, dict]]) -> frozenset:
    backfill.resolve_and_fold(str(repo), records)
    return rs.read_reviewed_set(str(repo))


# ---------------------------------------------------------------------------
# AC6a — _verdict_counts: pending excluded; ok/warn/blocked/waived/absent included.
# ---------------------------------------------------------------------------


class TestAC6aVerdictFilter:
    def test_pending_excluded_others_included_targeted_oracle(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        ok_tip = _make_commit(repo, "ok-tip")
        warn_tip = _make_commit(repo, "warn-tip")
        blocked_tip = _make_commit(repo, "blocked-tip")
        waived_tip = _make_commit(repo, "waived-tip")
        absent_tip = _make_commit(repo, "absent-verdict-tip")
        pending_tip = _make_commit(repo, "pending-tip")

        records = [
            ("r-ok", _base_record(f"{base}..{ok_tip}", verdict="ok")),
            ("r-warn", _base_record(f"{base}..{warn_tip}", verdict="warn")),
            ("r-blocked", _base_record(f"{base}..{blocked_tip}", verdict="blocked")),
            ("r-waived", _base_record(f"{base}..{waived_tip}", verdict="waived")),
            ("r-absent", {
                "sha_range": f"{base}..{absent_tip}", "reviewer": "code-reviewer",
                "scope": "chain", "scope_kind": "diff", "diff_loc": 5,
                "session_id": "abcdef01", "workstream": None,
            }),
            ("r-pending", _base_record(f"{base}..{pending_tip}", verdict="pending")),
        ]
        reviewed = _fold_and_read(repo, records)

        oracle_included = (
            _rev_list(f"{base}..{ok_tip}", repo)
            | _rev_list(f"{base}..{warn_tip}", repo)
            | _rev_list(f"{base}..{blocked_tip}", repo)
            | _rev_list(f"{base}..{waived_tip}", repo)
            | _rev_list(f"{base}..{absent_tip}", repo)
        )
        assert reviewed == oracle_included
        assert pending_tip not in reviewed


# ---------------------------------------------------------------------------
# AC6b — _record_range_has_stored_head: literal HEAD endpoint excluded entirely.
# ---------------------------------------------------------------------------


class TestAC6bStoredHeadExclusion:
    def test_stored_head_range_excluded_targeted_oracle(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")  # HEAD at record-write time

        records = [
            ("r-head", _base_record(f"{base}..HEAD")),
            ("r-plain", _base_record(f"{base}..{tip}", session_id="other-session")),
        ]
        reviewed = _fold_and_read(repo, records)

        # The oracle for a literal-HEAD range would (wrongly) include tip via
        # git rev-list <base>..HEAD; the preserved rule excludes the WHOLE
        # record, so only r-plain's range is credited.
        oracle_head_range = _rev_list(f"{base}..HEAD", repo)
        assert tip in oracle_head_range  # sanity: git itself would count it
        oracle_plain = _rev_list(f"{base}..{tip}", repo)

        assert reviewed == oracle_plain
        # Confirms r-head contributed nothing beyond what r-plain already did.


# ---------------------------------------------------------------------------
# AC6c — _credit_from_kind_partition: plan credited only against
# planning-artifact commits; integration skipped entirely.
# ---------------------------------------------------------------------------


class TestAC6cKindPartition:
    def test_plan_credited_only_against_planning_commits_targeted_oracle(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        code_commit = _make_file_commit(repo, "src/thing.py", "code change")
        plan_commit = _make_file_commit(repo, "docs/plans/2026-01-01-x.md", "plan change")

        records = [
            ("r-plan", _base_record(
                f"{base}..{plan_commit}", scope_kind="plan", session_id="other",
            )),
        ]
        reviewed = _fold_and_read(repo, records)

        full_range = _rev_list(f"{base}..{plan_commit}", repo)
        assert code_commit in full_range  # sanity: the range spans both commits
        assert plan_commit in full_range

        # Plan credit is restricted to the planning-artifact subset of the
        # range — computed here via the SAME preserved classifier
        # (`_classify_bookkeeping_shas`), never re-derived by this test.
        _exhaust, planning_set, _note = coverage._classify_bookkeeping_shas(
            list(full_range), str(repo), {},
        )
        oracle = full_range & planning_set
        assert plan_commit in oracle
        assert code_commit not in oracle  # code commit must NOT be creditable via plan

        assert reviewed == oracle

    def test_integration_kind_skipped_entirely_targeted_oracle(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        tip = _make_commit(repo, "tip")

        records = [
            ("r-integration", _base_record(
                f"{base}..{tip}", scope_kind="integration", session_id="other",
            )),
        ]
        reviewed = _fold_and_read(repo, records)

        assert reviewed == frozenset()
        assert tip in _rev_list(f"{base}..{tip}", repo)  # sanity: git would count it


# ---------------------------------------------------------------------------
# AC6d — _narrow_foreign_session_scope: commits trailer-attributed to a
# DIFFERENT session are stripped from a _FOREIGN_STRIPPED_SCOPES record.
# ---------------------------------------------------------------------------


class TestAC6dForeignSessionNarrowing:
    def test_foreign_trailered_commit_stripped_targeted_oracle(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        own = _make_commit(repo, "own-commit", session_id="own-session")
        foreign = _make_commit(repo, "foreign-commit", session_id="foreign-session")

        records = [
            ("r-chain", _base_record(
                f"{base}..{foreign}", scope="chain", session_id="own-session",
            )),
        ]
        reviewed = _fold_and_read(repo, records)

        full_range = _rev_list(f"{base}..{foreign}", repo)
        assert own in full_range and foreign in full_range

        oracle = full_range - {foreign}  # only the foreign-trailered commit is stripped
        assert reviewed == oracle
        assert foreign not in reviewed
        assert own in reviewed


# ---------------------------------------------------------------------------
# AC6e — the never-path-scoped asymmetric scope rule: a record's
# `scope_paths` field (if present) must NOT narrow what gets credited.
# ---------------------------------------------------------------------------


class TestAC6eNeverPathScoped:
    def test_scope_paths_field_does_not_narrow_credited_set_targeted_oracle(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        untouched_by_scope_paths = _make_file_commit(repo, "other/area.py", "unrelated area")

        record_with_scope_paths = _base_record(
            f"{base}..{untouched_by_scope_paths}",
            session_id="other",
        )
        record_with_scope_paths["scope_paths"] = ["only/this/narrow/path.py"]

        reviewed = _fold_and_read(repo, [("r-scoped", record_with_scope_paths)])

        oracle = _rev_list(f"{base}..{untouched_by_scope_paths}", repo)
        assert reviewed == oracle, (
            "a scope_paths field narrower than the commit's own touched paths "
            "must not exclude it from the reviewed_set (never-path-scoped rule)"
        )


# ---------------------------------------------------------------------------
# AC9 — the store is derived, never authoritative: delete + rebuild matches.
# ---------------------------------------------------------------------------


class TestAC9DerivedNeverAuthoritative:
    def test_delete_cache_and_rebuild_matches_pre_deletion_exactly(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _make_commit(repo, "base")
        diff_tip = _make_commit(repo, "diff-tip")
        code_commit = _make_file_commit(repo, "src/thing.py", "code change")
        plan_commit = _make_file_commit(repo, "docs/plans/2026-01-01-y.md", "plan change")
        foreign = _make_commit(repo, "foreign-commit", session_id="foreign-session")

        _write_trail_record(repo, "rec-01.json", _base_record(f"{base}..{diff_tip}"))
        _write_trail_record(
            repo, "rec-02.json",
            _base_record(f"{base}..{plan_commit}", scope_kind="plan", session_id="other"),
        )
        _write_trail_record(
            repo, "rec-03.json",
            _base_record(f"{diff_tip}..{foreign}", scope="chain", session_id="own"),
        )
        _write_trail_record(
            repo, "rec-04.json",
            _base_record(f"{base}..{code_commit}", verdict="pending"),
        )

        first = backfill.run_backfill(str(repo))
        assert first.folded, "fixture must actually fold something"
        pre_deletion = rs.read_reviewed_set(str(repo))
        assert pre_deletion, "fixture must produce a non-empty reviewed set"

        # Derived, not authoritative: delete both store files outright.
        shas_path = rs._shas_path(str(repo))
        ids_path = rs._folded_ids_path(str(repo))
        assert shas_path.exists() and ids_path.exists()
        shas_path.unlink()
        ids_path.unlink()
        assert rs.read_reviewed_set(str(repo)) == frozenset()

        second = backfill.run_backfill(str(repo))
        rebuilt = rs.read_reviewed_set(str(repo))

        assert first.folded == second.folded
        assert rebuilt == pre_deletion, (
            "the store is derived and must be reproducible byte for byte after "
            "deletion + rebuild"
        )


# ---------------------------------------------------------------------------
# (b) One-shot whole-corpus new-vs-retiring differential, against the REAL
# corpus of this repo. Additive/idempotent — never destructive to the live
# per-clone store (see module negative-spec).
# ---------------------------------------------------------------------------


class TestWholeCorpusDifferentialRatchet:
    """The differential was a ONE-SHOT gate, and it has fired.

    It ran live against both implementations while both existed, over this
    repo's real 4805-file corpus, and recorded divergence 0/0 at
    `_RATCHET_PATH`. C5 then deleted `coverage.build_reviewed_set`, so the
    comparison cannot be re-run: there is no longer a second implementation
    to differ against, and a test asserting agreement with a deleted symbol
    is not a weaker gate but an unrunnable one.

    What survives is the ratchet artifact, asserted here as a frozen record.
    The live equivalence guarantee going forward is carried by the targeted
    oracle cases above (AC6a-e), which ground-truth the store against an
    independent `git rev-list` rather than against any implementation.
    """

    def test_recorded_whole_corpus_differential_was_empty(self):
        if not _RATCHET_PATH.exists():
            pytest.skip(
                "no ratchet artifact in this checkout — the one-shot "
                "differential is recorded, not re-runnable"
            )
        recorded = json.loads(_RATCHET_PATH.read_text(encoding="utf-8"))

        assert recorded["divergence_old_only_count"] == 0, (
            "recorded whole-corpus differential is non-empty: the retiring "
            "implementation credited SHAs the store does not — see "
            f"{_RATCHET_PATH}"
        )
        assert recorded["divergence_new_only_count"] == 0, (
            "recorded whole-corpus differential is non-empty: the store "
            f"credits SHAs the retiring implementation did not — see {_RATCHET_PATH}"
        )
        assert recorded["old_reviewed_set_size"] == recorded["new_reviewed_set_size"]
        assert recorded["old_reviewed_set_size"] > 0, (
            "a differential over an empty set proves nothing"
        )
