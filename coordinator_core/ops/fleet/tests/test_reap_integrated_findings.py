"""
coordinator_core.ops.fleet.tests.test_reap_integrated_findings

Tests for the fleet.reap_integrated_findings op (DR-218 leg (a) of the
review-trail cleanup split — marker-PRESENT sidecars, no age gate).

Import guard: coordinator_core.ops.fleet.reap_integrated_findings MUST be
imported at module load time to fire the @register_op side-effect and
populate _REGISTRY. Without this import the decorator has not run and any
completeness test passes vacuously over an empty registry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  - op registered.
  - marker-present sidecar reaped regardless of age (0-day-old); marker-absent
    aged sidecar NOT reaped by this op (that's leg (b)'s remit).
  - dry_run:true mutates nothing.
  - dry_run omitted / wrong-type -> fail-closed exit_code:1, mutates nothing (AC5).
  - act-time re-verify: marker removed between scan and act -> skipped with
    reason containing "terminality-drift", never reaped.
  - history-preserving: git log finds a reaped TRACKED path.
  - state/review-trail/*.json parent-dir trail records untouched.
  - missing findings dir / not-a-repo -> nothing-to-do, exit 0.
  - idempotent second act run -> clean no-op.
  - tracked/untracked split (AC6/AC11): untracked marker-present sidecar
    reaped via plain unlink (reaped[], never failed[]); tracked marker-present
    sidecar reaped via git rm + commit (git log finds the path).
  - disjointness (AC7): a fixture with one marker-present and one
    marker-absent-and-aged sidecar — fleet.reap_integrated_findings reaps
    ONLY the marker-present one; fleet.reap_unintegrated_findings reaps ONLY
    the marker-absent-aged one.

Spec backlinks:
  - Plan: docs/plans/2026-07-14-review-trail-reaper-consolidation.md (chunk C3)
  - DR-218: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md
  - Pattern mirrors: test_reap_unintegrated_findings.py (fleet_repo conftest
    fixture, _head_sha / _seed_finding / _rel / _run / _iso_name helpers,
    import-guard convention).
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.fleet  # noqa: F401 — triggers package __init__
import coordinator_core.ops.fleet.reap_integrated_findings  # noqa: F401 — fires @register_op
import coordinator_core.ops.fleet.reap_unintegrated_findings  # noqa: F401 — disjointness test

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet._findings_reap import (
    reap_findings,
    scan_findings,
)
from coordinator_core.ops.fleet.reap_integrated_findings import (
    _handler,
    classify_integrated,
)
from coordinator_core.ops.fleet.reap_unintegrated_findings import (
    _handler as _unintegrated_handler,
)

_OP_NAME = "fleet.reap_integrated_findings"
_fleet_registered_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
assert len(_fleet_registered_ops) >= 1, (
    "import guard failed: no fleet.* ops in _REGISTRY — "
    "check coordinator_core.ops.fleet.reap_integrated_findings import"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.fleet.reap_integrated_findings @register_op did not fire"
)


def test_op_registered():
    """fleet.reap_integrated_findings must be in the registry after the import guard fires."""
    fleet_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
    assert len(fleet_ops) >= 1, (
        f"floor: at least one fleet.* op must be registered; got {sorted(fleet_ops)}"
    )
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} must be registered; present ops: {sorted(fleet_ops)}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _head_sha(fleet_repo) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


_FINDINGS_DIR_PARTS = ("state", "review-trail", "findings")


def _findings_dir(fleet_repo) -> Path:
    d = fleet_repo.root.joinpath(*_FINDINGS_DIR_PARTS)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_finding(fleet_repo, name: str, body: str = "Body text.\n") -> Path:
    """Write and commit a state/review-trail/findings/<name> sidecar."""
    d = _findings_dir(fleet_repo)
    path = d / name
    path.write_text(f"# Finding\n\n{body}", encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add finding {name}")
    return path


def _seed_finding_untracked(fleet_repo, name: str, body: str = "Body text.\n") -> Path:
    """Write (but do NOT commit) a state/review-trail/findings/<name> sidecar."""
    d = _findings_dir(fleet_repo)
    path = d / name
    path.write_text(f"# Finding\n\n{body}", encoding="utf-8")
    return path


def _seed_finding_with_marker(fleet_repo, name: str) -> Path:
    return _seed_finding(
        fleet_repo,
        name,
        body="Body text.\n\n## Integrator Dispositions\n\nDisposition notes.\n",
    )


def _seed_finding_with_marker_untracked(fleet_repo, name: str) -> Path:
    return _seed_finding_untracked(
        fleet_repo,
        name,
        body="Body text.\n\n## Integrator Dispositions\n\nDisposition notes.\n",
    )


def _rel(fleet_repo, name: str) -> str:
    return f"state/review-trail/findings/{name}"


def _iso_name(d: date, slug: str) -> str:
    return f"{d.isoformat()}-{slug}.md"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _dry_run_candidate_ids(fleet_repo) -> list:
    result = _run(_handler({"dry_run": True}, repo_root=fleet_repo.common_dir))
    return [c["id"] for c in result["candidates"]]


# ---------------------------------------------------------------------------
# Marker-present reaped regardless of age; marker-absent+aged NOT reaped here
# ---------------------------------------------------------------------------


def test_marker_present_zero_day_old_reaped(fleet_repo):
    """A 0-day-old (today-dated) marker-present sidecar IS reaped — no age gate."""
    today = _today()
    name = _iso_name(today, "fresh-integrated")
    _seed_finding_with_marker(fleet_repo, name)

    ids = _dry_run_candidate_ids(fleet_repo)
    assert _rel(fleet_repo, name) in ids, (
        "marker-present sidecar must be reaped regardless of age, incl. 0-day-old"
    )


def test_marker_absent_very_old_not_reaped_by_this_op(fleet_repo):
    """A very-old (400d) marker-ABSENT sidecar is NOT reaped by this op —
    that's leg (b)'s (fleet.reap_unintegrated_findings) remit."""
    old = _today() - timedelta(days=400)
    name = _iso_name(old, "very-old-unintegrated")
    _seed_finding(fleet_repo, name)

    ids = _dry_run_candidate_ids(fleet_repo)
    assert _rel(fleet_repo, name) not in ids, (
        "marker-absent sidecar must never be reaped by leg (a), regardless of age"
    )


# ---------------------------------------------------------------------------
# dry_run mutates nothing / fail-closed
# ---------------------------------------------------------------------------


def test_dry_run_mutates_nothing(fleet_repo):
    name = _iso_name(_today(), "dry-run-check")
    path = _seed_finding_with_marker(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(_handler({"dry_run": True}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert _rel(fleet_repo, name) in [c["id"] for c in result["candidates"]]
    assert result["reaped"] == []
    assert result["skipped"] == []
    assert result["failed"] == []

    assert path.exists()
    assert _head_sha(fleet_repo) == head_before
    assert fleet_repo.git_status_clean()


def test_dry_run_omitted_fails_closed_mutates_nothing(fleet_repo):
    """dry_run OMITTED from params -> exit_code:1, mutates nothing (AC5)."""
    name = _iso_name(_today(), "dry-run-omitted")
    path = _seed_finding_with_marker(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(_handler({}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 1
    assert result["dry_run"] is False
    assert result["candidates"] == []
    assert result["reaped"] == []
    assert result["skipped"] == []
    assert result["failed"] == []

    assert path.exists(), "dry_run-omitted call must not delete anything"
    assert _head_sha(fleet_repo) == head_before
    assert fleet_repo.git_status_clean()


def test_dry_run_wrong_type_fails_closed(fleet_repo):
    """dry_run passed as a non-bool (e.g. the string 'false') fails closed too (AC5)."""
    name = _iso_name(_today(), "dry-run-wrong-type")
    path = _seed_finding_with_marker(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(_handler({"dry_run": "false"}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 1
    assert result["dry_run"] is False
    assert result["candidates"] == []
    assert result["reaped"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    assert path.exists()
    assert _head_sha(fleet_repo) == head_before
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# Act-time re-verify
# ---------------------------------------------------------------------------


def test_act_time_marker_removed_after_scan_skipped_terminality_drift(fleet_repo):
    """A candidate whose marker is REMOVED between scan and act is skipped
    with reason containing 'terminality-drift', never reaped."""
    name = _iso_name(_today(), "drift-race")
    path = _seed_finding_with_marker(fleet_repo, name)
    rel = _rel(fleet_repo, name)

    scanned = [p for p, _ in scan_findings(fleet_repo.root, classify_integrated)]
    assert path.resolve() in [p.resolve() for p in scanned]

    # Simulate the marker being removed during the race window — rewrite the
    # file to strip the marker so classify_integrated's act-time re-check sees
    # marker-absent.
    path.write_text("# Finding\n\nBody text.\n", encoding="utf-8")

    reaped, skipped, failed = _run(
        reap_findings(
            fleet_repo.root,
            scanned,
            classify_integrated,
            subject_builder=lambda n: f"fleet: reap {n} integrated review-findings sidecar(s)",
        )
    )

    assert reaped == []
    assert failed == []
    skip_map = {s["id"]: s["reason"] for s in skipped}
    assert rel in skip_map
    assert "terminality-drift" in skip_map[rel]
    assert path.exists()


# ---------------------------------------------------------------------------
# History-preserving delete for TRACKED sidecars
# ---------------------------------------------------------------------------


def test_history_preserving_git_log_finds_reaped_path(fleet_repo):
    name = _iso_name(_today(), "history-check")
    path = _seed_finding_with_marker(fleet_repo, name)
    rel = _rel(fleet_repo, name)

    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 0
    assert not path.exists()

    log = subprocess.run(
        ["git", "log", "--oneline", "--", rel],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert log.strip() != "", f"git log must find the reaped path {rel!r}"


# ---------------------------------------------------------------------------
# Parent-dir trail records untouched
# ---------------------------------------------------------------------------


def test_parent_dir_json_trail_records_untouched(fleet_repo):
    """state/review-trail/*.json records in the PARENT dir are untouched after
    a reap — scoping to findings/*.md must actually exclude them."""
    name = _iso_name(_today(), "aged-with-sibling-json")
    _seed_finding_with_marker(fleet_repo, name)

    parent_json = fleet_repo.root / "state" / "review-trail" / "2026-07-01-some-trail-record.json"
    parent_json.parent.mkdir(parents=True, exist_ok=True)
    parent_json.write_text('{"key": "value"}', encoding="utf-8")
    fleet_repo._git("add", str(parent_json))
    fleet_repo._git("commit", "-m", "add parent trail json")

    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 0

    assert parent_json.exists(), "parent-dir JSON trail record must be untouched"
    assert parent_json.read_text(encoding="utf-8") == '{"key": "value"}'


# ---------------------------------------------------------------------------
# Missing findings dir / not-a-repo -> nothing-to-do
# ---------------------------------------------------------------------------


def test_missing_findings_dir_dry_run_nothing_to_do(fleet_repo):
    result = _run(_handler({"dry_run": True}, repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 0
    assert result["candidates"] == []


def test_missing_findings_dir_act_nothing_to_do(fleet_repo):
    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 0
    assert result["reaped"] == []
    assert result["skipped"] == []
    assert result["failed"] == []


def test_degrades_to_missing_findings_dir_handler_nothing_to_do(tmp_path):
    """A common_dir whose derived worktree has no state/review-trail/findings/
    tree at all degrades to the same nothing-to-do result as a missing
    findings dir."""
    common_dir = tmp_path / "fake" / ".git"
    common_dir.mkdir(parents=True)

    preview = _run(_handler({"dry_run": True}, repo_root=common_dir))
    assert preview["exit_code"] == 0
    assert preview["candidates"] == []

    act = _run(_handler({"dry_run": False}, repo_root=common_dir))
    assert act["exit_code"] == 0
    assert act["reaped"] == []
    assert act["skipped"] == []
    assert act["failed"] == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_second_act_run_noop(fleet_repo):
    name = _iso_name(_today(), "idem-target")
    path = _seed_finding_with_marker(fleet_repo, name)

    first = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert first["exit_code"] == 0
    assert not path.exists()
    head_after_first = _head_sha(fleet_repo)

    second = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert second["exit_code"] == 0
    assert second["reaped"] == []
    assert second["skipped"] == []
    assert second["failed"] == []

    assert _head_sha(fleet_repo) == head_after_first, "no empty commit on second act run"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# Tracked/untracked split (AC6/AC11)
# ---------------------------------------------------------------------------


def test_untracked_marker_present_reaped_via_unlink(fleet_repo):
    """A marker-present sidecar created-and-marked but NEVER git-committed
    (UNTRACKED) is reaped via plain unlink, exit_code 0, appears in reaped[],
    does NOT appear in failed[]."""
    name = _iso_name(_today(), "untracked-integrated")
    path = _seed_finding_with_marker_untracked(fleet_repo, name)
    rel = _rel(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    assert not path.exists()
    reaped_ids = [r["id"] for r in result["reaped"]]
    failed_ids = [f["id"] for f in result["failed"]]
    assert rel in reaped_ids
    assert rel not in failed_ids
    assert result["failed"] == []

    # Plain unlink of an untracked file must not produce a commit.
    assert _head_sha(fleet_repo) == head_before, "unlinking an untracked file must not commit"
    assert fleet_repo.git_status_clean()


def test_tracked_marker_present_reaped_via_git_rm_and_commit(fleet_repo):
    """A TRACKED marker-present sidecar is reaped via git rm + commit —
    git log finds the reaped path afterward."""
    name = _iso_name(_today(), "tracked-integrated")
    path = _seed_finding_with_marker(fleet_repo, name)
    rel = _rel(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    assert not path.exists()
    reaped_ids = [r["id"] for r in result["reaped"]]
    assert rel in reaped_ids
    assert result["failed"] == []

    assert _head_sha(fleet_repo) != head_before, "a commit must land for the tracked delete"

    log = subprocess.run(
        ["git", "log", "--oneline", "--", rel],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert log.strip() != "", f"git log must find the reaped tracked path {rel!r}"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# Disjointness (AC7): leg (a) and leg (b) reap disjoint sets
# ---------------------------------------------------------------------------


def test_legs_reap_disjoint_sets(fleet_repo):
    """A fixture with ONE marker-present sidecar AND one marker-absent-aged
    sidecar: fleet.reap_integrated_findings reaps ONLY the marker-present
    one; fleet.reap_unintegrated_findings reaps ONLY the marker-absent-aged
    one — no overlap."""
    old = _today() - timedelta(days=30)
    integrated_name = _iso_name(_today(), "disjoint-integrated")
    unintegrated_name = _iso_name(old, "disjoint-unintegrated")

    integrated_path = _seed_finding_with_marker(fleet_repo, integrated_name)
    unintegrated_path = _seed_finding(fleet_repo, unintegrated_name)

    integrated_rel = _rel(fleet_repo, integrated_name)
    unintegrated_rel = _rel(fleet_repo, unintegrated_name)

    integrated_result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert integrated_result["exit_code"] == 0
    integrated_reaped_ids = [r["id"] for r in integrated_result["reaped"]]
    assert integrated_rel in integrated_reaped_ids
    assert unintegrated_rel not in integrated_reaped_ids
    assert not integrated_path.exists()
    assert unintegrated_path.exists(), "leg (a) must not touch the marker-absent-aged sidecar"

    unintegrated_result = _run(
        _unintegrated_handler({"dry_run": False}, repo_root=fleet_repo.common_dir)
    )
    assert unintegrated_result["exit_code"] == 0
    unintegrated_reaped_ids = [r["id"] for r in unintegrated_result["reaped"]]
    assert unintegrated_rel in unintegrated_reaped_ids
    assert integrated_rel not in unintegrated_reaped_ids
    assert not unintegrated_path.exists()

    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# subject_prefix (C1)
# ---------------------------------------------------------------------------


def test_subject_prefix_passthrough_prepended_to_commit_subject(fleet_repo):
    """A valid str subject_prefix is prepended verbatim (bare concatenation —
    no auto-inserted separator) to the commit subject."""
    name = _iso_name(_today(), "subject-prefix-check")
    _seed_finding_with_marker(fleet_repo, name)

    result = _run(
        _handler({"dry_run": False, "subject_prefix": "foo: "}, repo_root=fleet_repo.common_dir)
    )
    assert result["exit_code"] == 0
    assert result["failed"] == []

    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    assert subject.startswith("foo: "), f"commit subject must start with prefix; got {subject!r}"
    assert fleet_repo.git_status_clean()


def test_subject_prefix_non_str_fails_closed(fleet_repo):
    """A non-str subject_prefix (e.g. int) fails closed: exit_code:1, mutates nothing."""
    name = _iso_name(_today(), "subject-prefix-wrong-type")
    path = _seed_finding_with_marker(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(
        _handler({"dry_run": False, "subject_prefix": 123}, repo_root=fleet_repo.common_dir)
    )

    assert result["exit_code"] == 1
    assert result["dry_run"] is False
    assert result["candidates"] == []
    assert result["reaped"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    assert path.exists()
    assert _head_sha(fleet_repo) == head_before
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# Multi-file reap (C3)
# ---------------------------------------------------------------------------


def test_multi_file_reap_two_tracked_sidecars_one_commit(fleet_repo):
    """TWO tracked marker-present sidecars reaped in one act -> both land in
    reaped[], commit subject reads '...2 integrated...', git log --name-only
    lists both paths."""
    name_a = _iso_name(_today(), "multi-file-a")
    name_b = _iso_name(_today(), "multi-file-b")
    _seed_finding_with_marker(fleet_repo, name_a)
    _seed_finding_with_marker(fleet_repo, name_b)
    rel_a = _rel(fleet_repo, name_a)
    rel_b = _rel(fleet_repo, name_b)

    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 0
    assert result["failed"] == []

    reaped_ids = [r["id"] for r in result["reaped"]]
    assert rel_a in reaped_ids
    assert rel_b in reaped_ids

    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    assert "2 integrated" in subject, f"commit subject must mention '2 integrated'; got {subject!r}"

    name_only = subprocess.run(
        ["git", "log", "--name-only", "-1"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert rel_a in name_only
    assert rel_b in name_only
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# D3 repo_root mismatch (C2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# summary_limit (F8) — caps candidates/reaped and adds *_total count fields;
# omitted -> full lists, no *_total keys (byte-identical to pre-F8 shape).
# ---------------------------------------------------------------------------


def test_summary_limit_caps_dry_run_candidates_and_adds_total(fleet_repo):
    for i in range(4):
        name = _iso_name(_today(), f"summary-limit-{i}")
        _seed_finding_with_marker(fleet_repo, name)

    result = _run(
        _handler({"dry_run": True, "summary_limit": 2}, repo_root=fleet_repo.common_dir)
    )

    assert result["exit_code"] == 0
    assert len(result["candidates"]) == 2
    assert result["candidates_total"] == 4


def test_summary_limit_omitted_no_total_keys(fleet_repo):
    name = _iso_name(_today(), "no-summary-limit")
    _seed_finding_with_marker(fleet_repo, name)

    result = _run(_handler({"dry_run": True}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    assert "candidates_total" not in result


def test_summary_limit_caps_act_reaped_and_exit_code_uses_untruncated_failed(fleet_repo):
    for i in range(3):
        name = _iso_name(_today(), f"summary-limit-act-{i}")
        _seed_finding_with_marker(fleet_repo, name)

    result = _run(
        _handler({"dry_run": False, "summary_limit": 1}, repo_root=fleet_repo.common_dir)
    )

    assert result["exit_code"] == 0
    assert len(result["reaped"]) == 1
    assert result["reaped_total"] == 3
    assert result["failed_total"] == 0


def test_summary_limit_non_positive_int_fails_closed(fleet_repo):
    name = _iso_name(_today(), "summary-limit-bad")
    path = _seed_finding_with_marker(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(
        _handler({"dry_run": False, "summary_limit": 0}, repo_root=fleet_repo.common_dir)
    )

    assert result["exit_code"] == 1
    assert result["reaped"] == []
    assert result["failed"] == []
    assert path.exists()
    assert _head_sha(fleet_repo) == head_before


def test_summary_limit_wrong_type_fails_closed(fleet_repo):
    name = _iso_name(_today(), "summary-limit-wrong-type")
    path = _seed_finding_with_marker(fleet_repo, name)
    head_before = _head_sha(fleet_repo)

    result = _run(
        _handler({"dry_run": True, "summary_limit": "2"}, repo_root=fleet_repo.common_dir)
    )

    assert result["exit_code"] == 1
    assert result["candidates"] == []
    assert path.exists()
    assert _head_sha(fleet_repo) == head_before


def test_d3_repo_root_mismatch_fails_closed(fleet_repo, tmp_path):
    """params.repo_root pointing at a DIFFERENT git repo than the engine-
    supplied common_dir -> D3 mismatch branch, exit_code:1."""
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args), cwd=str(other_repo), capture_output=True, check=True
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "o@o.com")
    _git("config", "user.name", "Other")
    _git("config", "commit.gpgsign", "false")
    _git("commit", "--allow-empty", "-m", "init")

    result = _run(
        _handler(
            {"dry_run": True, "repo_root": str(other_repo)},
            repo_root=fleet_repo.common_dir,
        )
    )

    assert result["exit_code"] == 1
    assert result["candidates"] == []
    assert result["reaped"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    assert fleet_repo.git_status_clean()
