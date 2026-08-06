"""
coordinator_core.ops.fleet.tests.test_reap_unintegrated_findings

Tests for the fleet.reap_unintegrated_findings op (DR-218 leg (b) of the
review-trail cleanup split).

Import guard: coordinator_core.ops.fleet.reap_unintegrated_findings MUST be
imported at module load time to fire the @register_op side-effect and
populate _REGISTRY.  Without this import the decorator has not run and any
completeness test passes vacuously over an empty registry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage — predicate truth table + date tiers + destructive-op cases:
  - marker-present sidecar (aged) -> KEPT.
  - marker-absent + <14d -> KEPT.
  - marker-absent + aged -> REAPED.
  - one case per date-extraction tier (tier1 leading-ISO incl T-suffixed form,
    tier2 leading compact, tier3 embedded), all reaped when aged.
  - genuinely date-less filename -> KEPT (fail-closed-to-keep).
  - dry_run:true mutates nothing.
  - idempotent: second act run with nothing left -> clean no-op.
  - cwd-scope guards: missing findings dir / not-a-git-repo -> nothing-to-do.
  - history-preserving: after reap, git log finds the reaped path.
  - (a) exact-boundary age: 13d-ago KEPT vs 14d-ago REAPED (off-by-one guard).
  - (b) parent-dir state/review-trail/*.json trail records untouched (AC7).
  - (c) commit-failure worktree-restoration for the delete case.
  - (d) marker-anchoring: indented / code-fenced-inline "## Integrator
        Dispositions" must NOT count as marker-present.
  - (e) act-time re-verify / leg-(a) fold-race: a candidate that gains the
        marker between _scan_reapable and _reap is skipped, never reaped.
  - (f) dry_run omitted -> fail-closed exit_code:1, mutates nothing (slice2 F1 /
        the destructive-act-by-default regression this guards).
  - (g) invalid-calendar-date tier fallthrough: a filename that syntactically
        matches a tier but fails date() construction (e.g. month 13) is KEPT.
  - (h) commutativity: reaping [A, B] vs [B, A] in independent fixtures produces
        the same final repo state (DR-218 D2(ii)).

Spec backlinks:
  - Plan: docs/plans/2026-07-14-review-findings-aged-unintegrated-reaper.md
  - DR-218: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md
  - Pattern mirrors: test_archive_shipped_handoffs.py (fleet_repo conftest fixture,
    _head_sha helper, import guard).
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
import coordinator_core.ops.fleet.reap_unintegrated_findings  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.reap_unintegrated_findings import (
    _handler,
    _reap,
    _scan_reapable,
)

_OP_NAME = "fleet.reap_unintegrated_findings"
_fleet_registered_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
assert len(_fleet_registered_ops) >= 1, (
    "import guard failed: no fleet.* ops in _REGISTRY — "
    "check coordinator_core.ops.fleet.reap_unintegrated_findings import"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.fleet.reap_unintegrated_findings @register_op did not fire"
)


def test_op_registered():
    """fleet.reap_unintegrated_findings must be in the registry after the import guard fires."""
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


def _seed_finding_with_marker(fleet_repo, name: str) -> Path:
    return _seed_finding(
        fleet_repo,
        name,
        body="Body text.\n\n## Integrator Dispositions\n\nDisposition notes.\n",
    )


def _rel(fleet_repo, name: str) -> str:
    return f"state/review-trail/findings/{name}"


def _iso_name(d: date, slug: str) -> str:
    return f"{d.isoformat()}-{slug}.md"


def _t_suffixed_name(d: date, slug: str) -> str:
    return f"{d.isoformat()}T125656Z-{slug}.md"


def _compact_name(d: date, slug: str) -> str:
    return f"{d.strftime('%Y%m%d')}-{slug}.md"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _dry_run_candidate_ids(fleet_repo) -> list:
    result = _run(_handler({"dry_run": True}, repo_root=fleet_repo.common_dir))
    return [c["id"] for c in result["candidates"]]


# ---------------------------------------------------------------------------
# Predicate truth table
# ---------------------------------------------------------------------------


def test_marker_present_aged_kept(fleet_repo):
    """A marker-present sidecar is KEPT even when aged past the threshold."""
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "integrated-old")
    _seed_finding_with_marker(fleet_repo, name)

    ids = _dry_run_candidate_ids(fleet_repo)
    assert _rel(fleet_repo, name) not in ids, (
        "marker-present sidecar must never be reaped — it belongs to example-doctrine-repo's leg (a)"
    )


def test_marker_absent_fresh_kept(fleet_repo):
    """A marker-absent sidecar younger than 14 days is KEPT (not yet aged)."""
    fresh = _today() - timedelta(days=1)
    name = _iso_name(fresh, "fresh-unintegrated")
    _seed_finding(fleet_repo, name)

    ids = _dry_run_candidate_ids(fleet_repo)
    assert _rel(fleet_repo, name) not in ids


def test_marker_absent_aged_reaped(fleet_repo):
    """A marker-absent sidecar older than 14 days is REAPED."""
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "aged-unintegrated")
    _seed_finding(fleet_repo, name)

    ids = _dry_run_candidate_ids(fleet_repo)
    assert _rel(fleet_repo, name) in ids


# ---------------------------------------------------------------------------
# Date-extraction tier coverage — all reaped when aged
# ---------------------------------------------------------------------------


def test_tier1_leading_iso_reaped(fleet_repo):
    old = _today() - timedelta(days=20)
    name = _iso_name(old, "tier1-plain")
    _seed_finding(fleet_repo, name)
    assert _rel(fleet_repo, name) in _dry_run_candidate_ids(fleet_repo)


def test_tier1_leading_iso_t_suffixed_reaped(fleet_repo):
    """Tier 1 also matches the T-suffixed form: 2026-07-12T125656Z-....md."""
    old = _today() - timedelta(days=20)
    name = _t_suffixed_name(old, "tier1-tsuffix")
    _seed_finding(fleet_repo, name)
    assert _rel(fleet_repo, name) in _dry_run_candidate_ids(fleet_repo)


def test_tier2_leading_compact_reaped(fleet_repo):
    """Tier 2: leading compact YYYYMMDD-... form."""
    old = _today() - timedelta(days=20)
    name = _compact_name(old, "tier2-compact")
    _seed_finding(fleet_repo, name)
    assert _rel(fleet_repo, name) in _dry_run_candidate_ids(fleet_repo)


def test_tier3_embedded_leading_prefix_reaped(fleet_repo):
    """Tier 3: embedded ISO date with a leading non-date prefix (wsc-...)."""
    old = _today() - timedelta(days=20)
    name = f"wsc-{old.isoformat()}-something.md"
    _seed_finding(fleet_repo, name)
    assert _rel(fleet_repo, name) in _dry_run_candidate_ids(fleet_repo)


def test_tier3_embedded_trailing_suffix_reaped(fleet_repo):
    """Tier 3: embedded ISO date at the end of the filename (the Director of Engineering-...-DATE.md)."""
    old = _today() - timedelta(days=20)
    name = f"the Director of Engineering-note-{old.isoformat()}.md"
    _seed_finding(fleet_repo, name)
    assert _rel(fleet_repo, name) in _dry_run_candidate_ids(fleet_repo)


def test_dateless_filename_kept(fleet_repo):
    """A filename with no tier match is KEPT (fail-closed-to-keep)."""
    name = "no-date-here-findings.md"
    _seed_finding(fleet_repo, name)
    assert _rel(fleet_repo, name) not in _dry_run_candidate_ids(fleet_repo)


# ---------------------------------------------------------------------------
# dry_run / idempotency / scope guards
# ---------------------------------------------------------------------------


def test_dry_run_mutates_nothing(fleet_repo):
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "dry-run-check")
    path = _seed_finding(fleet_repo, name)
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


def test_idempotent_second_act_run_noop(fleet_repo):
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "idem-target")
    path = _seed_finding(fleet_repo, name)

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


def test_missing_findings_dir_dry_run_nothing_to_do(fleet_repo):
    """No state/review-trail/findings/ dir at all -> clean empty dry_run result."""
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
    tree at all degrades to the same nothing-to-do result as a missing findings
    dir (Review: code-reviewer — slice4 F3: this test does not actually exercise
    git-repo-ness — there is no git check anywhere in this op; renamed to
    describe the missing-findings-dir degrade path it verifies)."""
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


def test_history_preserving_git_log_finds_reaped_path(fleet_repo):
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "history-check")
    path = _seed_finding(fleet_repo, name)
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
# (a) exact-boundary age
# ---------------------------------------------------------------------------


def test_boundary_13d_kept_vs_14d_reaped(fleet_repo):
    """A sidecar authored 13 days ago is KEPT vs one authored exactly 14 days
    ago is REAPED — verifies the '<= today-14d' semantics is not off-by-one."""
    today = _today()
    kept_date = today - timedelta(days=13)
    reaped_date = today - timedelta(days=14)

    kept_name = _iso_name(kept_date, "boundary-kept")
    reaped_name = _iso_name(reaped_date, "boundary-reaped")

    _seed_finding(fleet_repo, kept_name)
    _seed_finding(fleet_repo, reaped_name)

    ids = _dry_run_candidate_ids(fleet_repo)

    assert _rel(fleet_repo, kept_name) not in ids, (
        "13 days ago must be KEPT — not yet aged past the 14-day threshold"
    )
    assert _rel(fleet_repo, reaped_name) in ids, (
        "exactly 14 days ago must be REAPED — the boundary is inclusive (<=today-14d)"
    )


# ---------------------------------------------------------------------------
# (b) parent-dir trail records untouched (AC7)
# ---------------------------------------------------------------------------


def test_parent_dir_json_trail_records_untouched(fleet_repo):
    """state/review-trail/*.json records in the PARENT dir are untouched after
    a reap — scoping to findings/*.md must actually exclude them."""
    old = _today() - timedelta(days=30)
    finding_name = _iso_name(old, "aged-with-sibling-json")
    _seed_finding(fleet_repo, finding_name)

    parent_json = fleet_repo.root / "state" / "review-trail" / "2026-07-01-some-trail-record.json"
    parent_json.parent.mkdir(parents=True, exist_ok=True)
    parent_json.write_text('{"key": "value"}', encoding="utf-8")
    fleet_repo._git("add", str(parent_json))
    fleet_repo._git("commit", "-m", "add parent trail json")

    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))
    assert result["exit_code"] == 0

    assert parent_json.exists(), "parent-dir JSON trail record must be untouched (AC7)"
    assert parent_json.read_text(encoding="utf-8") == '{"key": "value"}'


# ---------------------------------------------------------------------------
# (c) commit-failure worktree-restoration for the DELETE case
# ---------------------------------------------------------------------------


def test_commit_failure_restores_reaped_sidecar(fleet_repo):
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "commitfail-sidecar")
    path = _seed_finding(fleet_repo, name)
    content = path.read_text(encoding="utf-8")
    rel = _rel(fleet_repo, name)

    hooks_dir = fleet_repo.root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook_path.chmod(0o755)

    head_before = _head_sha(fleet_repo)

    result = _run(_handler({"dry_run": False}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 2
    assert result["reaped"] == []
    assert len(result["failed"]) == 1
    assert result["failed"][0]["id"] == rel
    assert "commit-failed" in result["failed"][0]["reason"]

    assert path.exists(), "reaped sidecar must be restored from HEAD after commit failure"
    assert path.read_text(encoding="utf-8") == content

    # Review: code-reviewer — slice4 F4: HEAD-unchanged assertion, mirroring the
    # _common sibling (test_commit_failure_restores_reaped_files_from_head) — no
    # commit must land when the commit itself fails.
    head_after = _head_sha(fleet_repo)
    assert head_after == head_before, "no commit must have landed when commit fails"


# ---------------------------------------------------------------------------
# (d) marker-anchoring — indented / code-fenced-inline must NOT count as present
# ---------------------------------------------------------------------------


def test_indented_marker_not_anchored_still_reaped(fleet_repo):
    """An INDENTED '## Integrator Dispositions' is not an anchored line-start
    heading — must NOT count as marker-present, so the sidecar is still reaped."""
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "indented-marker")
    body = "Body text.\n\n    ## Integrator Dispositions\n\n(indented — not a real heading)\n"
    _seed_finding(fleet_repo, name, body=body)

    assert _rel(fleet_repo, name) in _dry_run_candidate_ids(fleet_repo), (
        "indented '## Integrator Dispositions' must NOT anchor as marker-present"
    )


def test_code_fenced_inline_marker_not_anchored_still_reaped(fleet_repo):
    """A CODE-FENCED/inline '## Integrator Dispositions' (not alone on its own
    line) is not an anchored heading — must NOT count as marker-present."""
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "fenced-marker")
    body = "See `## Integrator Dispositions` mentioned inline, not a real heading.\n"
    _seed_finding(fleet_repo, name, body=body)

    assert _rel(fleet_repo, name) in _dry_run_candidate_ids(fleet_repo), (
        "inline/code-fenced '## Integrator Dispositions' must NOT anchor as marker-present"
    )


# ---------------------------------------------------------------------------
# (e) act-time re-verify / leg-(a) fold-race
# ---------------------------------------------------------------------------


def test_act_time_marker_gained_after_scan_skipped_terminality_drift(fleet_repo):
    """A candidate that GAINS the marker between _scan_reapable and _reap is
    skipped with reason containing 'terminality-drift', never reaped."""
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "fold-race")
    path = _seed_finding(fleet_repo, name)
    rel = _rel(fleet_repo, name)

    scanned = [p for p, _ in _scan_reapable(fleet_repo.root)]
    assert path.resolve() in [p.resolve() for p in scanned]

    # Simulate the marker landing during the race window (leg-(a) integration
    # fold) — write it directly to disk so _reap's content re-check sees it.
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text + "\n## Integrator Dispositions\n\nIntegrated during the race.\n",
        encoding="utf-8",
    )

    reaped, skipped, failed = _run(_reap(fleet_repo.root, scanned))

    assert reaped == []
    assert failed == []
    skip_map = {s["id"]: s["reason"] for s in skipped}
    assert rel in skip_map
    assert "terminality-drift" in skip_map[rel]


# ---------------------------------------------------------------------------
# (f) dry_run omitted -> fail-closed (slice2 F1 / A1)
# ---------------------------------------------------------------------------


def test_dry_run_omitted_fails_closed_mutates_nothing(fleet_repo):
    """dry_run OMITTED from params -> exit_code:1, mutates nothing.

    Review: code-reviewer — slice2 F1 / C3. Prior behavior silently defaulted
    an omitted dry_run to False, which would run the destructive ACT (git rm)
    path on any caller that forgot the param. The handler must fail closed
    instead: no dry_run -> exit_code:1 setup-error envelope, nothing touched.
    """
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "dry-run-omitted")
    path = _seed_finding(fleet_repo, name)
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
    """dry_run passed as a non-bool (e.g. the string 'false') fails closed too —
    not just outright omission (guards against a truthy/falsy-string caller bug)."""
    old = _today() - timedelta(days=30)
    name = _iso_name(old, "dry-run-wrong-type")
    path = _seed_finding(fleet_repo, name)

    result = _run(_handler({"dry_run": "false"}, repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 1
    assert result["reaped"] == []
    assert result["failed"] == []
    assert path.exists()


# ---------------------------------------------------------------------------
# (g) invalid-calendar-date tier fallthrough -> KEPT (fail-closed-to-keep)
# ---------------------------------------------------------------------------


def test_invalid_calendar_date_tier_fallthrough_kept(fleet_repo):
    """A filename that syntactically matches a date tier but fails date()
    construction (month 13, day 40) -> KEPT (fail-closed-to-keep).

    "2026-13-40-badmonth.md" matches tier 1's \\d{4}-\\d{2}-\\d{2} shape but
    date(2026, 13, 40) raises ValueError; no other tier match exists elsewhere
    in the name, so _extract_authored_date must return None and the file must
    be kept, never reaped.
    """
    name = "2026-13-40-badmonth.md"
    _seed_finding(fleet_repo, name)
    assert _rel(fleet_repo, name) not in _dry_run_candidate_ids(fleet_repo)


# ---------------------------------------------------------------------------
# (h) commutativity: reap order does not affect final repo state (DR-218 D2(ii))
# ---------------------------------------------------------------------------


def _build_bare_fleet_repo(root: Path) -> Path:
    """Build a minimal standalone git repo at root (no FleetRepo wrapper) for the
    commutativity test, which needs two fully independent repos in one test."""
    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
        )

    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main")
    _git("config", "user.email", "commute-test@claude-klabauter.test")
    _git("config", "user.name", "Commute Test")
    _git("config", "commit.gpgsign", "false")

    d = root / "state" / "review-trail" / "findings"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")
    return root


def _seed_bare_finding(root: Path, name: str) -> Path:
    path = root / "state" / "review-trail" / "findings" / name
    path.write_text("# Finding\n\nBody text.\n", encoding="utf-8")
    subprocess.run(["git", "add", str(path)], cwd=str(root), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"add finding {name}"],
        cwd=str(root), capture_output=True, check=True,
    )
    return path


def test_reap_order_commutative(tmp_path):
    """Reaping [A, B] in one fixture and [B, A] in a fresh independent fixture
    leaves the same final repo state — DR-218 D2(ii): the reap operation must
    not be sensitive to candidate ordering."""
    old = _today() - timedelta(days=30)
    name_a = _iso_name(old, "commute-a")
    name_b = _iso_name(old, "commute-b")

    root_ab = _build_bare_fleet_repo(tmp_path / "repo-ab")
    path_a_ab = _seed_bare_finding(root_ab, name_a)
    path_b_ab = _seed_bare_finding(root_ab, name_b)
    reaped_ab, skipped_ab, failed_ab = _run(_reap(root_ab, [path_a_ab, path_b_ab]))

    root_ba = _build_bare_fleet_repo(tmp_path / "repo-ba")
    path_a_ba = _seed_bare_finding(root_ba, name_a)
    path_b_ba = _seed_bare_finding(root_ba, name_b)
    reaped_ba, skipped_ba, failed_ba = _run(_reap(root_ba, [path_b_ba, path_a_ba]))

    assert sorted(r["id"] for r in reaped_ab) == sorted(r["id"] for r in reaped_ba)
    assert skipped_ab == skipped_ba == []
    assert failed_ab == failed_ba == []

    assert not path_a_ab.exists() and not path_b_ab.exists()
    assert not path_a_ba.exists() and not path_b_ba.exists()

    status_ab = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root_ab), capture_output=True,
    ).stdout.strip()
    status_ba = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root_ba), capture_output=True,
    ).stdout.strip()
    assert status_ab == b"" == status_ba
