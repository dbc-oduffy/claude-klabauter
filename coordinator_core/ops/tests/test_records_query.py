"""
coordinator_core.ops.tests.test_records_query — smoke tests for "records.query" op (C1a).

Purpose: Verify happy-path loading, op registration, full ``--where`` grammar
filtering (equality AND the T4d-g1c EXTEND operators) with quoted YAML values,
and the empty-payload guard on unknown type.  Does NOT include differential
parity tests against ``query-records.js`` (that is C1b's remit — see
test_records_query_parity.py).

Import guard: ``import coordinator_core.ops`` MUST precede all test functions
so that ALL op registrations fire before any test assertion.

Coverage:
  (a) registry-completeness — registry is non-empty after coordinator_core.ops import
  (b) op-registered         — "records.query" is in the registry
  (c) query_quoted_roadmap_id — equality filter matches quoted YAML values
      (AC2, anti-read_fm_field regression: ``roadmap_id: "claude-klabauter-strangler-2026-07-04"``
      must match the clause ``roadmap_id=claude-klabauter-strangler-2026-07-04``)
  (d) unknown_type_loud_exit — unknown type → SystemExit(1) + stderr naming valid types
      (fail-loud, trips PATH-fallback; inverted from old AC2 fail-open)
  (e) no_repo_root_empty_payload — repo_root=None → empty payload, no raise
  (f) full --where grammar operators — !=, <, >, <=, >=, in(...), bare-field
      exists, byte-parity per freeze-query-records-grammar.md Surface 3 (T4d-g1c)
  (g) liveness() predicate table — spot checks against the freeze manifest's
      frozen mapping (full sweep lives in test_records_query_parity.py)

Spec backlink: docs/plans/2026-07-06-strang-11-c11-12-records-query-op.md § C1a
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4d-g1c
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from coordinator_core.testing.doe_root import resolve_doe_root

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "records.query".  MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.records_query import (
    _ARCHIVE_GLOB_FOR_TYPE,
    _LEGACY_PROSE_ENTRY_LINE_RE,
    _TYPE_DISPLAY,
    _TYPE_TO_GLOB,
    _apply_plan_filename_filter,
    _collect_files,
    _collect_handoff_ledger_records,
    _collect_research_claim_records,
    _collect_type_records,
    _handler,
    _load_record,
    _normalize_roadmap_status,
    _parse_handoff_ledger_blocks,
    _parse_where,
    _walk_glob_segments,
    liveness,
)
from coordinator_core.write_guards.nudge_improvement_queue_write import (
    _ENTRY_LINE_RE as _WRITE_GUARD_ENTRY_LINE_RE,
)

# ---------------------------------------------------------------------------
# Registry completeness assertion (universal positive floor)
# ---------------------------------------------------------------------------

assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)

_OP_NAME = "records.query"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.records_query @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Execute a coroutine synchronously (test helper).

    ``_handler`` is a plain ``def`` (no ``await`` in its body — see
    ``test_async_handler_discipline.py``), so calls already resolve to a
    plain value by the time they reach here; pass those through unchanged.
    """
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root`` and return its common_dir (.git path)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "records-query-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Records Query Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    return (root / ".git").resolve()


def _write_handoff(
    handoffs_dir: Path,
    filename: str,
    *,
    kind: str = "spinoff-roadmap",
    roadmap_id: str,
    deployment_state: str = "awaiting_gate",
    extra_fields: str = "",
    body: str = "Body content.",
) -> Path:
    """Write a minimal handoff .md file with YAML frontmatter (quoted roadmap_id)."""
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / filename
    # Use YAML quoted string for roadmap_id — the key regression this test guards.
    content = dedent(f"""\
        ---
        kind: {kind}
        roadmap_id: "{roadmap_id}"
        deployment_state: {deployment_state}
        {extra_fields}
        ---
        {body}
    """)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_repo(tmp_path: Path):
    """A minimal git repo with a few handoff files.

    Tree:
      state/handoffs/
        hoff-match-1.md   — kind=spinoff-roadmap, roadmap_id="claude-klabauter-strangler-2026-07-04"
        hoff-match-2.md   — kind=spinoff-roadmap, roadmap_id="claude-klabauter-strangler-2026-07-04"
        hoff-no-match.md  — kind=spinoff-roadmap, roadmap_id="other-roadmap-xyz"

    Returns (repo_root_git_dir, worktree_root).
    """
    worktree = tmp_path / "repo"
    git_dir = _make_git_repo(worktree)
    handoffs_dir = worktree / "state" / "handoffs"

    _write_handoff(
        handoffs_dir,
        "hoff-match-1.md",
        roadmap_id="claude-klabauter-strangler-2026-07-04",
    )
    _write_handoff(
        handoffs_dir,
        "hoff-match-2.md",
        roadmap_id="claude-klabauter-strangler-2026-07-04",
        deployment_state="ready_to_fire",
    )
    _write_handoff(
        handoffs_dir,
        "hoff-no-match.md",
        roadmap_id="other-roadmap-xyz",
    )

    return git_dir, worktree


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestQueryQuotedRoadmapId:
    """AC2 / C1a smoke: equality filter works on quoted YAML roadmap_id values.

    This is the primary regression guard: ``read_fm_field`` returns the raw YAML
    text (including quotes), so ``roadmap_id: "foo"`` would mis-match the clause
    ``roadmap_id=foo``.  ``_parse_frontmatter`` resolves the quoted string to the
    Python str ``"foo"``, so the match works correctly.
    """

    def test_matching_records_returned(self, tmp_repo):
        git_dir, worktree = tmp_repo
        result = _run(
            _handler(
                params={
                    "type": "handoff",
                    "where": "kind=spinoff-roadmap AND roadmap_id=claude-klabauter-strangler-2026-07-04",
                    "format": "paths",
                },
                repo_root=git_dir,
            )
        )
        records = result["records"]
        # Two files match (hoff-match-1.md and hoff-match-2.md).
        assert isinstance(records, str)
        paths = [p for p in records.split("\n") if p]
        assert len(paths) == 2
        basenames = {Path(p).name for p in paths}
        assert "hoff-match-1.md" in basenames
        assert "hoff-match-2.md" in basenames
        assert "hoff-no-match.md" not in basenames

    def test_non_matching_roadmap_id_excluded(self, tmp_repo):
        git_dir, worktree = tmp_repo
        result = _run(
            _handler(
                params={
                    "type": "handoff",
                    "where": "roadmap_id=nonexistent-roadmap-id",
                    "format": "paths",
                },
                repo_root=git_dir,
            )
        )
        records = result["records"]
        assert records == "" or records == []

    def test_format_json_includes_frontmatter(self, tmp_repo):
        git_dir, worktree = tmp_repo
        result = _run(
            _handler(
                params={
                    "type": "handoff",
                    "where": "kind=spinoff-roadmap AND roadmap_id=claude-klabauter-strangler-2026-07-04",
                    "format": "json",
                },
                repo_root=git_dir,
            )
        )
        records = result["records"]
        assert isinstance(records, list)
        assert len(records) == 2
        for rec in records:
            assert "path" in rec
            assert "frontmatter" in rec
            fm = rec["frontmatter"]
            # Frontmatter dict must carry the dequoted roadmap_id value.
            assert fm.get("roadmap_id") == "claude-klabauter-strangler-2026-07-04"
            assert fm.get("kind") == "spinoff-roadmap"


class TestEmptyPayloadGuards:
    """Guard: unknown type → loud exit (trips PATH-fallback); absent repo_root → empty payload."""

    def test_unknown_type_exits_loud_paths(self, tmp_repo, capsys):
        """Unknown type causes SystemExit(1) with stderr naming valid types (format=paths)."""
        # Review: F10 — pass a real git_dir so only the unknown-type guard fires,
        # not the absent-repo_root guard.
        git_dir, _worktree = tmp_repo
        with pytest.raises(SystemExit) as exc_info:
            _run(
                _handler(
                    params={"type": "nonexistent-type", "format": "paths"},
                    repo_root=git_dir,
                )
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "nonexistent-type" in captured.err, (
            "stderr must name the unknown type"
        )
        assert "Valid" in captured.err, (
            "stderr must list valid types"
        )

    def test_unknown_type_exits_loud_json(self, capsys):
        """Unknown type causes SystemExit(1) with stderr naming valid types (format=json)."""
        with pytest.raises(SystemExit) as exc_info:
            _run(
                _handler(
                    params={"type": "nonexistent-type", "format": "json"},
                    repo_root=None,
                )
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "nonexistent-type" in captured.err
        assert "Valid" in captured.err

    def test_unknown_param_key_exits_loud_with_did_you_mean(self, tmp_repo, capsys):
        """Kebab-case `older-than` → SystemExit(1) + stderr naming the key and
        suggesting the canonical snake_case spelling — never a silently-dropped
        filter returning the unfiltered superset with exit 0."""
        git_dir, _worktree = tmp_repo
        with pytest.raises(SystemExit) as exc_info:
            _run(
                _handler(
                    params={"type": "handoff", "older-than": "9999d"},
                    repo_root=git_dir,
                )
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "older-than" in captured.err, "stderr must name the unknown key"
        assert "older_than" in captured.err, (
            "stderr must suggest the nearest known key (did-you-mean)"
        )

    def test_unknown_param_key_no_close_match_still_exits_loud(self, tmp_repo, capsys):
        """A key with no near-miss (bogus_param_xyz) still fail-louds, listing
        the valid param set without a did-you-mean line."""
        git_dir, _worktree = tmp_repo
        with pytest.raises(SystemExit) as exc_info:
            _run(
                _handler(
                    params={"type": "handoff", "bogus_param_xyz": "zzz"},
                    repo_root=git_dir,
                )
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "bogus_param_xyz" in captured.err
        assert "Valid" in captured.err, "stderr must list valid param keys"

    def test_all_known_param_keys_accepted(self, tmp_repo):
        """The full canonical param set passes the allowlist (no SystemExit)."""
        git_dir, _worktree = tmp_repo
        result = _run(
            _handler(
                params={
                    "type": "handoff",
                    "where": "status=gated",
                    "since": "1d",
                    "older_than": "9999d",
                    "sort": "-created",
                    "format": "json",
                    "limit": 5,
                    "unattached": False,
                },
                repo_root=git_dir,
            )
        )
        assert "records" in result

    def test_no_repo_root_returns_empty(self):
        result = _run(
            _handler(
                params={"type": "handoff", "format": "paths"},
                repo_root=None,
            )
        )
        # Well-formed empty payload, no raise.
        assert "records" in result
        assert result["records"] == "" or result["records"] == []


# ---------------------------------------------------------------------------
# Tests (F2 smoke): boolean coercion parity — Python True/False → 'true'/'false'
# ---------------------------------------------------------------------------


class TestBooleanCoercion:
    """F2 regression: _matches_where coerces Python bools to lowercase to match JS String(true).

    str(True) == 'True' in Python, but JS String(true) == 'true'.
    A where clause draft=true must match a record with `draft: true` frontmatter.
    """

    def test_bool_field_true_matches(self, tmp_path: Path):
        """draft=true query matches a handoff with `draft: true` YAML frontmatter."""
        # Review: F2 — smoke test for boolean coercion parity
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        handoffs_dir = worktree / "state" / "handoffs"

        # Write a handoff with a boolean `draft: true` field.
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        (handoffs_dir / "draft-handoff.md").write_text(
            "---\nkind: spinoff-roadmap\nroadmap_id: test-rmap\n"
            "deployment_state: awaiting_gate\ndraft: true\n---\nBody.\n",
            encoding="utf-8",
        )

        result = _run(
            _handler(
                params={"type": "handoff", "where": "draft=true", "format": "paths"},
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        assert len(paths) == 1, (
            f"Expected 1 match for draft=true, got {len(paths)}: {paths}. "
            "Check _matches_where bool coercion (str(True)='True' vs 'true')."
        )
        assert "draft-handoff.md" in Path(paths[0]).name

    def test_bool_field_false_matches(self, tmp_path: Path):
        """draft=false query matches a handoff with `draft: false` YAML frontmatter."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        handoffs_dir = worktree / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)

        (handoffs_dir / "nondraft-handoff.md").write_text(
            "---\nkind: spinoff-roadmap\nroadmap_id: test-rmap\n"
            "deployment_state: awaiting_gate\ndraft: false\n---\nBody.\n",
            encoding="utf-8",
        )
        (handoffs_dir / "draft-handoff.md").write_text(
            "---\nkind: spinoff-roadmap\nroadmap_id: test-rmap\n"
            "deployment_state: awaiting_gate\ndraft: true\n---\nBody.\n",
            encoding="utf-8",
        )

        result = _run(
            _handler(
                params={"type": "handoff", "where": "draft=false", "format": "paths"},
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        assert len(paths) == 1
        assert "nondraft-handoff.md" in Path(paths[0]).name


# ---------------------------------------------------------------------------
# Tests (T4d-g1c): full --where grammar operator support
#
# Pre-T4d-g1c, these operators (!=, <, >, <=, >=, in) were REJECTED with
# sys.exit(1) — the op only supported equality-AND conjunctions. T4d-g1c
# EXTENDS records_query.py to the full query-records.js grammar (freeze-
# query-records-grammar.md Surface 3), so these operators are now SUPPORTED,
# not rejected. Only a genuinely unparseable clause still exits loud.
# ---------------------------------------------------------------------------


class TestWhereGrammarOperators:
    """T4d-g1c: full --where grammar — !=, <, >, <=, >=, in(...), bare-field exists.

    Byte-parity port of query-records.js's parseClause/matchesClause (freeze-
    query-records-grammar.md Surface 3), including compareValues' numeric-first-
    then-string-fallback trap and parseClause's operator scan order.
    """

    def test_ne_operator_parses_and_filters(self):
        """!= operator is parsed (not rejected) and yields the correct clause shape."""
        result = _parse_where("deployment_state!=ready_to_fire")
        assert result == [{"field": "deployment_state", "op": "!=", "value": "ready_to_fire"}]

    def test_lt_operator_parses(self):
        result = _parse_where("count<5")
        assert result == [{"field": "count", "op": "<", "value": "5"}]

    def test_gt_operator_parses(self):
        result = _parse_where("count>5")
        assert result == [{"field": "count", "op": ">", "value": "5"}]

    def test_lte_operator_parses(self):
        result = _parse_where("count<=5")
        assert result == [{"field": "count", "op": "<=", "value": "5"}]

    def test_gte_operator_parses(self):
        result = _parse_where("count>=5")
        assert result == [{"field": "count", "op": ">=", "value": "5"}]

    def test_in_operator_parses(self):
        """field in (a,b,c) — the parenthesised-list shape, checked before the scalar scan."""
        result = _parse_where("kind in (spinoff-roadmap,spinoff-goal)")
        assert result == [
            {"field": "kind", "op": "in", "values": ["spinoff-roadmap", "spinoff-goal"]}
        ]

    def test_bare_field_presence_filter(self):
        """A bare field name (no operator) becomes an 'exists' presence filter."""
        result = _parse_where("origin_goal_id")
        assert result == [{"field": "origin_goal_id", "op": "exists"}]

    def test_genuinely_unparseable_clause_still_exits(self, capsys):
        """A clause matching NEITHER an operator NOR a bare-\\w+ field name still exits(1)."""
        with pytest.raises(SystemExit) as exc_info:
            _parse_where("not a valid clause shape")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err

    def test_numeric_gt_is_numeric_not_lexicographic(self, tmp_path: Path):
        """priority>3 returns priority=10 (NOT lexicographic '10'<'3') — compareValues trap.

        Pins the exact byte-behavior freeze-query-records-grammar.md Surface 3 calls out
        as "the trap for a naive port": a string-only comparator would exclude priority=10
        from a >3 filter because the string '10' sorts before '3'.
        """
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        handoffs_dir = worktree / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        (handoffs_dir / "low-priority.md").write_text(
            "---\nkind: spinoff-roadmap\nroadmap_id: x\npriority: 2\n"
            "deployment_state: awaiting_gate\n---\nBody.\n",
            encoding="utf-8",
        )
        (handoffs_dir / "high-priority.md").write_text(
            "---\nkind: spinoff-roadmap\nroadmap_id: x\npriority: 10\n"
            "deployment_state: awaiting_gate\n---\nBody.\n",
            encoding="utf-8",
        )

        result = _run(
            _handler(
                params={"type": "handoff", "where": "priority>3", "format": "paths"},
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        basenames = {Path(p).name for p in paths}
        assert "high-priority.md" in basenames, (
            "priority=10 must match priority>3 numerically — lexicographic "
            "compare would wrongly exclude it ('10' < '3' as strings)"
        )
        assert "low-priority.md" not in basenames


class TestLivenessPredicateSmoke:
    """T4d-g1c: spot checks against freeze-query-records-grammar.md Surface 2's
    liveness() table. Full 13-type-plus-graceful-default sweep lives in the
    differential parity suite; these are always-on (no node dependency).
    """

    def test_handoff_two_axis(self):
        assert liveness({"deployment_state": "awaiting_gate"}, "handoff") == "BLOCKED"
        # DR-084: status: claimed is the current vocabulary; status: consumed is
        # the retired predecessor, kept here as an old-name tolerance check —
        # _TERMINAL_STATUS is old-union-new widened (lifecycle_constants.py).
        assert liveness({"status": "claimed"}, "handoff") == "DONE"
        assert liveness({"status": "consumed"}, "handoff") == "DONE"
        assert liveness({"deployment_state": "shipped"}, "handoff") == "DONE"
        assert liveness({}, "handoff") == "LIVE"

    def test_roadmap_blocked_status_is_live_not_blocked(self):
        """NEGATIVE SPEC: roadmap status='blocked' maps to LIVE, not BLOCKED."""
        assert liveness({"status": "blocked"}, "roadmap") == "LIVE"
        assert liveness({"status": "shipped"}, "roadmap") == "DONE"

    def test_plan_ignores_deployment_state(self):
        assert liveness({"status": "deferred", "deployment_state": "shipped"}, "plan") == "BLOCKED"
        assert liveness({"status": "implemented"}, "plan") == "DONE"

    def test_health_status_keys_on_status_not_health(self):
        """health-status liveness keys on fm.status (lifecycle), NOT fm.health (posture)."""
        assert liveness({"status": "active", "health": "red"}, "health-status") == "LIVE"
        assert liveness({"status": "archived", "health": "green"}, "health-status") == "DONE"

    def test_graceful_default_for_unwired_type(self):
        assert liveness({}, "handoff-ledger") == "LIVE"
        assert liveness({"status": "claimed"}, "handoff-ledger") == "DONE"
        # Old-name tolerance: _TERMINAL_STATUS is old-union-new widened.
        assert liveness({"status": "consumed"}, "handoff-ledger") == "DONE"


# ---------------------------------------------------------------------------
# Tests (F9): smoke coverage for type=plan sidecar exclusion + consumed-marker
# ---------------------------------------------------------------------------


class TestPlanTypeSmoke:
    """F9a: always-on smoke test for type=plan positive canonical allowlist.

    These are the always-on behavioral net when differential parity tests skip
    (e.g. node not available). Verifies the sidecar exclusion logic is live.

    Review: F9 — coverage gaps; parity suite was only guard for these paths.
    """

    def test_canonical_plan_included_sidecar_excluded(self, tmp_path: Path):
        """type=plan query includes canonical plans and excludes known sidecar variants."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        plans_dir = worktree / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)

        # Canonical plan — must be included.
        (plans_dir / "2026-07-01-my-plan.md").write_text(
            "---\nstatus: implemented\n---\nPlan body.\n", encoding="utf-8"
        )
        # Timestamped sidecar — must be excluded.
        (plans_dir / "2026-07-01-my-plan.plan-coverage-check.2026-07-01T08-00-00Z.md").write_text(
            "---\nstatus: implemented\n---\nSidecar body.\n", encoding="utf-8"
        )
        # Classic sidecar — must be excluded.
        (plans_dir / "2026-07-01-my-plan.prior-art-check.md").write_text(
            "---\nstatus: implemented\n---\nPrior-art sidecar.\n", encoding="utf-8"
        )

        result = _run(
            _handler(
                params={"type": "plan", "where": "status=implemented", "format": "paths"},
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        basenames = {Path(p).name for p in paths}

        assert "2026-07-01-my-plan.md" in basenames, (
            "Canonical plan should be included in type=plan query"
        )
        for name in basenames:
            assert ".plan-coverage-check." not in name, (
                f"Sidecar leaked into type=plan results: {name}"
            )
            assert ".prior-art-check." not in name, (
                f"Sidecar leaked into type=plan results: {name}"
            )


class TestConsumedMarkerSmoke:
    """F9b: always-on smoke test for consumed-marker normalization.

    Verifies _apply_consumed_marker normalizes deployment_state → shipped BEFORE
    the --where filter, so a consumed-marker-lagged handoff is excluded from
    deployment_state=ready_to_fire queries even though its raw frontmatter says
    ready_to_fire.

    Review: F9 — this path had zero always-on coverage.
    """

    def test_consumed_marker_handoff_excluded_from_ready_to_fire(self, tmp_path: Path):
        """Consumed-marker-lagged handoff is excluded from deployment_state=ready_to_fire."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        handoffs_dir = worktree / "state" / "handoffs"

        # Consumed-marker-lagged: frontmatter says ready_to_fire, body has consumed marker.
        # applyConsumedMarker must normalize to shipped BEFORE --where filtering.
        _write_handoff(
            handoffs_dir,
            "consumed-lagged.md",
            roadmap_id="test-rmap",
            deployment_state="ready_to_fire",
            body="<!-- consumed: 2026-07-01 -->\nConsumed handoff body.",
        )

        # Non-consumed ready_to_fire: should still appear.
        _write_handoff(
            handoffs_dir,
            "live-rtf.md",
            roadmap_id="test-rmap",
            deployment_state="ready_to_fire",
        )

        result = _run(
            _handler(
                params={
                    "type": "handoff",
                    "where": "deployment_state=ready_to_fire",
                    "format": "paths",
                },
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        basenames = {Path(p).name for p in paths}

        assert "consumed-lagged.md" not in basenames, (
            "_apply_consumed_marker did not normalize deployment_state → shipped "
            "before --where filter; consumed-lagged handoff incorrectly included."
        )
        assert "live-rtf.md" in basenames, (
            "Non-consumed ready_to_fire handoff should be included in results."
        )


# ---------------------------------------------------------------------------
# Directory-scan failure — distinguishability from legitimate-empty (silent-
# success audit, state/audits/2026-07-22-silent-success-audit.md)
# ---------------------------------------------------------------------------


class TestDirectoryScanFailureSignal:
    """An unreadable records directory must NOT look like "zero records exist".

    Before this fix, ``_collect_files``'s ``except OSError: return []`` made a
    genuine scan failure indistinguishable from a legitimately-empty result —
    both silently produced ``{"records": []}`` / ``{"records": ""}``. Callers
    (ledgers/audits/dashboards) need a checkable signal to tell them apart.
    """

    def test_unreadable_handoffs_dir_signals_incomplete_not_empty(self, tmp_path: Path):
        import os
        import sys

        if sys.platform.startswith("win") or os.geteuid() == 0:
            pytest.skip(
                "directory-permission enforcement is unreliable as root / on Windows"
            )

        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        handoffs_dir = worktree / "state" / "handoffs"
        _write_handoff(handoffs_dir, "hoff-1.md", roadmap_id="test-rmap")

        handoffs_dir.chmod(0o000)
        try:
            result = _run(
                _handler(
                    params={"type": "handoff", "format": "json"},
                    repo_root=git_dir,
                )
            )
        finally:
            handoffs_dir.chmod(0o755)

        assert result["records"] == [], (
            "unreadable dir should still yield a well-formed empty records list"
        )
        assert result.get("incomplete") is True, (
            "an unreadable handoffs dir must set incomplete=True — otherwise "
            "this scan failure is indistinguishable from a legitimately-empty "
            "result, which is exactly the silent-success bug this test guards."
        )
        assert result.get("error"), (
            "an unreadable handoffs dir must carry a non-empty 'error' "
            "diagnostic naming what could not be scanned."
        )


# ---------------------------------------------------------------------------
# Legacy prose-queue invisibility signal (DR-115 —
# docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md,
# example-doctrine-repo repo). Six sibling repos still carry pre-migration line-per-row prose
# queues that were previously silently unread by this query — this signal
# makes that invisibility loud rather than indistinguishable from "empty".
# ---------------------------------------------------------------------------


class TestLegacyProseQueueSignal:
    """`records.query --type improvement` must not silently mask an unmigrated
    legacy prose queue as "zero improvement entries".
    """

    def test_yaml_only_no_legacy_signal(self, tmp_path: Path):
        """Regression guard: a repo with ONLY the per-entry YAML directory
        (the fully-migrated shape) must not gain the legacy signal or have its
        record count shift."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        d = worktree / "state" / "improvement-queue"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: Example improvement\nstatus: open\n", encoding="utf-8",
        )

        result = _run(
            _handler(params={"type": "improvement", "format": "json"}, repo_root=git_dir)
        )

        assert len(result["records"]) == 1
        assert "legacy_prose_unindexed_count" not in result
        assert "legacy_prose_unindexed_path" not in result
        assert "legacy_prose_unindexed_remediation" not in result

    def test_legacy_only_signals_count_and_path(self, tmp_path: Path):
        """A non-empty legacy prose queue with NO per-entry YAML directory
        must surface the signal, with the correct count and path."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        (worktree / "state").mkdir(parents=True)
        (worktree / "state" / "improvement-queue.md").write_text(
            "# Improvement Queue\n"
            "- 2026-07-01 | idea one | notes\n"
            "- 2026-07-02 | idea two | notes\n"
            "- 2026-07-03 | idea three | notes\n",
            encoding="utf-8",
        )

        result = _run(
            _handler(params={"type": "improvement", "format": "json"}, repo_root=git_dir)
        )

        assert result["records"] == []
        assert result["legacy_prose_unindexed_count"] == 3
        assert result["legacy_prose_unindexed_path"] == "state/improvement-queue.md"
        assert "migrate-improvement-queue-project.py" in result["legacy_prose_unindexed_remediation"]

    def test_both_present_neither_masks_the_other(self, tmp_path: Path):
        """Both the YAML directory AND the legacy prose file populated: the
        YAML records are returned normally AND the legacy signal still fires —
        one must not mask the other."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        d = worktree / "state" / "improvement-queue"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: Example improvement\nstatus: open\n", encoding="utf-8",
        )
        (worktree / "state" / "improvement-queue.md").write_text(
            "- 2026-07-01 | idea one | notes\n"
            "- 2026-07-02 | idea two | notes\n",
            encoding="utf-8",
        )

        result = _run(
            _handler(params={"type": "improvement", "format": "json"}, repo_root=git_dir)
        )

        assert len(result["records"]) == 1, "the YAML-side record must still be returned"
        assert result["legacy_prose_unindexed_count"] == 2
        assert result["legacy_prose_unindexed_path"] == "state/improvement-queue.md"

    def test_empty_legacy_file_no_false_alarm(self, tmp_path: Path):
        """A legacy prose file that exists but has zero pipe-row entry lines
        (whitespace-only / tombstone note) must NOT fire the signal."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        (worktree / "state").mkdir(parents=True)
        (worktree / "state" / "improvement-queue.md").write_text(
            "   \n\n# Improvement Queue\n\nNothing here yet.\n", encoding="utf-8",
        )

        result = _run(
            _handler(params={"type": "improvement", "format": "json"}, repo_root=git_dir)
        )

        assert result["records"] == []
        assert "legacy_prose_unindexed_count" not in result
        assert "legacy_prose_unindexed_path" not in result
        assert "legacy_prose_unindexed_remediation" not in result

    def test_bug_type_legacy_signal(self, tmp_path: Path):
        """The signal is not improvement-only — `bug` has its own legacy path
        and its own remediation migrator named."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        (worktree / "state").mkdir(parents=True)
        (worktree / "state" / "bug-backlog.md").write_text(
            "- 2026-07-01 | some bug | notes\n", encoding="utf-8",
        )

        result = _run(_handler(params={"type": "bug", "format": "json"}, repo_root=git_dir))

        assert result["legacy_prose_unindexed_count"] == 1
        assert result["legacy_prose_unindexed_path"] == "state/bug-backlog.md"
        assert "migrate-bug-backlog.py" in result["legacy_prose_unindexed_remediation"]

    def test_debt_type_legacy_signal(self, tmp_path: Path):
        """The signal is not improvement/bug-only — `debt` has its own legacy
        path and its own remediation migrator named."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        (worktree / "state").mkdir(parents=True)
        (worktree / "state" / "debt-backlog.md").write_text(
            "- 2026-07-01 | some debt | notes\n", encoding="utf-8",
        )

        result = _run(_handler(params={"type": "debt", "format": "json"}, repo_root=git_dir))

        assert result["legacy_prose_unindexed_count"] == 1
        assert result["legacy_prose_unindexed_path"] == "state/debt-backlog.md"
        assert "migrate-debt-backlog.py" in result["legacy_prose_unindexed_remediation"]

    def test_type_without_legacy_path_never_signals(self, tmp_path: Path):
        """A type with no `_LEGACY_PROSE_QUEUE_PATH` entry (e.g. `lesson`) never
        carries the signal, even if a same-shaped file happens to exist at an
        unrelated path. `lesson` genuinely has no legacy prose queue leg —
        `debt` no longer qualifies as the neutral example now that it has a
        `_LEGACY_PROSE_QUEUE_PATH` entry (see Finding 1, DR-115)."""
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        (worktree / "state").mkdir(parents=True)
        (worktree / "state" / "lesson-backlog.md").write_text(
            "- 2026-07-01 | some lesson | notes\n", encoding="utf-8",
        )

        result = _run(_handler(params={"type": "lesson", "format": "json"}, repo_root=git_dir))

        assert "legacy_prose_unindexed_count" not in result

    def test_entry_line_regex_deliberately_diverges_from_write_guard(self):
        """`_LEGACY_PROSE_ENTRY_LINE_RE` used to be a byte-identical
        re-derivation of `nudge_improvement_queue_write._ENTRY_LINE_RE`
        (`^- \\d{4}-\\d{2}-\\d{2} \\|`) — that byte-identity is exactly what
        made the read-side signal dead against the real bug/debt-backlog
        corpus, which never uses the dated-pipe shape the write guard
        polices (see `_LEGACY_PROSE_ENTRY_LINE_RE`'s own module-level
        docstring for the full rationale and fleet measurement). This test
        pins the divergence so nobody "fixes" it back to a re-unification —
        the write guard's narrow single-shape regex is correct for ITS job
        (nudging a same-format append inside one Write/Edit payload); this
        module's regex needs multi-shape coverage for ITS job (counting
        entries in a whole real file accumulated across incompatible
        skill-authored conventions)."""
        # The write guard's one shape must still be a SUBSET the widened
        # regex recognises — an improvement-queue.md dated-pipe row (the one
        # shape both regexes agree is a real entry) must match both.
        dated_pipe_row = "- 2026-07-01 | self | notes | proposed target: x"
        assert _WRITE_GUARD_ENTRY_LINE_RE.search(dated_pipe_row)
        assert _LEGACY_PROSE_ENTRY_LINE_RE.search(dated_pipe_row)
        # Review: code-reviewer (Finding 3) — the divergence itself is proven
        # behaviorally: a real bug/debt-backlog table row the write guard's
        # narrow dated-pipe shape does NOT recognise, but this module's
        # widened regex does. A bare `.pattern != .pattern` string inequality
        # (removed here) proved nothing about actual matching behavior.
        table_id_row = "| BS-2026-06-14-11 | pipeline-extract-build | P2 | text | evidence |"
        assert not _WRITE_GUARD_ENTRY_LINE_RE.search(table_id_row)
        assert _LEGACY_PROSE_ENTRY_LINE_RE.search(table_id_row)

    @pytest.mark.parametrize(
        "line",
        [
            # markdown table row, ID-shaped first cell (bug/debt-backlog.md shape)
            "| BS-2026-06-14-11 | pipeline-extract-build | P2 | text | evidence |",
            "| DSR-2026-04-11-2 | RAG / NLM Intake | P3 | text | source | open | 2026-04-11 |",
            # struck-through closed table row (example-sim-repo-md bug-backlog.md shape)
            "| ~~BS-2026-04-09-1~~ | FDM | ~~P2~~ CLOSED | text | N/A | 2026-04-09 |",
            # short non-dated ID in a table (bug-backlog "Spun off" table shape)
            "| BS-030 | chunk_csharp_docs.py brace-counting | handoff path | reason |",
            # non-dated hyphenated ID with letter+digit segments, no date at all
            "| WAA-A-P0-1 | Routing | Middleware dead code |",
            "| TD-PARITY-PS1 | example-game-repo-docs | P3 | text | source | open |",
            # bulleted bold ALL-CAPS identifier (debt-backlog.md shape)
            "- **DSR-2026-06-16-1** [for-doc-sweep] text here",
            "- **F-C-02 false-positive lesson:** SQLite backslash text",
            "- **BS-2026-06-01-CLIRUNNER-EXECSYNC-BLANK-PROMPT** → handoff path",
            # bulleted bold all-lowercase multi-segment slug (spinoff-entry shape)
            "- **embed-sidecar-anyio-portal-flaky-crash** — the residual blocker",
            "- **json-retrieval-quality** — RESOLVED (commit abc123)",
            # unbolded bulleted ID-first pipe row (debt-backlog.md shape)
            "- DSR-2026-05-24-1 | 2026-05-24 | file.py | one-line description | target",
            # legacy dated-pipe bullet (improvement-queue.md shape, pre-existing)
            "- 2026-06-15 | self | one-line lesson | proposed target: wiki",
            # branch (a) with irregular/multi-space whitespace — deliberately
            # widened from the old single-space shape (`^- \d{4}-\d{2}-\d{2} \|`);
            # exercises the widened boundary rather than merely assuming it's
            # harmless. Review: code-reviewer (Finding 4).
            "-   2026-06-15   |  self | one-line lesson | proposed target: wiki",
        ],
    )
    def test_entry_line_regex_positive_shapes(self, line: str):
        """Every observed real row shape across the 16-file fleet corpus
        (bug-backlog.md / debt-backlog.md / improvement-queue.md) must match."""
        assert _LEGACY_PROSE_ENTRY_LINE_RE.search(line), f"expected a match: {line!r}"

    @pytest.mark.parametrize(
        "line",
        [
            # narrative bullet inside a prose section — no entry-identifier token
            "- The lesson: verify SQL backslash semantics empirically first.",
            "- Rejected: the sweeper's hypothesis does not survive engine source review.",
            # mixed-case natural-language bold lead-in — a lower-case segment
            # ("regressions retrospective") after the first hyphen means it's
            # not ALL-CAPS, so branch (c) can't match (digit presence in "2"
            # is irrelevant) — real fleet false-positive candidate: project-
            # rag-ue-addon bug-backlog.md "Notes" section.
            # Review: code-reviewer (Finding 1) — comment was swapped with
            # the C3-priming line below; each now describes its own line.
            "- **Round-2 regressions retrospective:** three round-1 fixes broke tests.",
            # short label starting with an ALL-CAPS-with-digit token ("C3")
            # followed by a lower-case segment ("priming") — same exclusion
            # mechanism as above (not ALL-CAPS throughout), NOT "no digit":
            # C3 plainly contains one. Real fleet false-positive candidate
            # (example-retrieval-repo bug-backlog.md summary section).
            "- **C3-priming:** 14/15 already-fixed, 1 file-removed.",
            "- **C1-core:** stale TODOs cited are now rationale comments.",
            # indented sub-bullet — must never match regardless of leading content
            "  - Same family — a flaky order-dependence issue, RESOLVED 2026-06-01.",
            "  - **BS-2026-06-01-CLIRUNNER**: an indented duplicate must not match.",
            # code-fenced schema placeholder — literal "YYYY-MM-DD", not real digits
            "- YYYY-MM-DD | <source-file>:<line> | <one-line lesson> | proposed target: <target>",
            # markdown table header / separator rows
            "| ID | System | Severity | Summary | Source | Status | Added |",
            "|----|--------|----------|---------|--------|--------|-------|",
            # markdown table continuation row with an empty first cell
            "| | | | UPDATE 2026-07-21: additional detail on a prior row. |",
        ],
    )
    def test_entry_line_regex_negative_shapes(self, line: str):
        """Narrative bullets, indented sub-bullets, code-fenced placeholder
        rows, and table header/separator/continuation rows must never be
        swept in — the whole point of widening this regex is to gain
        coverage without losing precision."""
        assert not _LEGACY_PROSE_ENTRY_LINE_RE.search(line), f"unexpected match: {line!r}"

    # Review: code-reviewer (Finding 2) — branch (b) (markdown-table ID cell)
    # has no digit requirement or case constraint, unlike its sibling ID
    # branches, so it has no adversarial negative coverage for its own
    # broadest failure mode. These document the current (accepted) false-
    # positive surface rather than asserting a fix — see the digit-lookahead
    # note on `_LEGACY_PROSE_ENTRY_LINE_RE`'s branch (b) comment for why the
    # lookahead was NOT added (it drops a real corpus row,
    # example-stats-repo/state/debt-backlog.md's `| G-OVR | ... |`).
    @pytest.mark.parametrize(
        "line",
        [
            # plausible hyphenated-but-non-ID first cell (glossary/legend-shaped
            # row) — CURRENTLY matches branch (b); no real corpus row does this
            # today, but the branch has no digit/case guard against it.
            "| high-priority | items flagged for immediate attention |",
            "| self-review | a reviewer checking their own prior work |",
        ],
    )
    def test_entry_line_regex_branch_b_false_positive_surface(self, line: str):
        """Documents branch (b)'s known-accepted false-positive surface: a
        hyphenated-but-non-ID glossary/legend table row currently counts as
        an entry. Not a regression guard — flip this assertion if branch (b)
        is ever tightened to exclude these."""
        assert _LEGACY_PROSE_ENTRY_LINE_RE.search(line), (
            f"expected branch (b) to currently match (documented false-"
            f"positive surface, not yet guarded): {line!r}"
        )


# ---------------------------------------------------------------------------
# T4d-g1c EXTEND — type-set widening (8 new types), .yaml whole-file parsing,
# wildcard-directory globs (roadmap/completion), and roadmap status
# normalization. Spec backlink: query-records.js _buildTypeToGlob
# (bin/query-records.js:211-272) and normalizeRoadmapStatus (:1052-1080).
# ---------------------------------------------------------------------------


class TestNewTypeGlobCoverage:
    """_TYPE_TO_GLOB carries the 8 T4d-g1c-widened types plus the 5 T4d-g1-recipe
    types (decision/review/lesson/handoff-ledger/research-claim) alongside the
    original 4, plus ``goal`` (added when a sibling repo's goal-coverage-scan
    port reported false-empty against this op — see module Negative-spec),
    plus ``research-synthesis``/``gap-report``/``coverage-audit`` (added
    2026-07-22 — 3 live example-doctrine-repo runtime consumers, same false-empty shape),
    plus ``archived-memo`` (added 2026-07-24 — JS-vs-native parity, C3),
    plus ``cutover`` (added 2026-07-25 — schema-recognised, genuine
    record-shaped collection with a live producer landing, same "wire it"
    precedent as goal/sizing-object; the glob needs arbitrary-depth ``**``
    support, see ``_walk_glob_segments``), plus ``priority-intent``/
    ``priority-ledger`` (added 2026-07-27 — schema-recognised, genuine
    record-shaped collections with live producers already landed
    (``priority.set``/``priority.drain``), same "wire it" precedent)."""

    def test_all_wired_types_present(self):
        expected = {
            "handoff", "handoff-archived", "plan", "cross-repo-memo",
            "bug", "debt", "improvement", "tracker", "roadmap",
            "health-status", "decision-guide", "completion",
            "decision", "review", "lesson", "handoff-ledger", "research-claim",
            "goal", "research-synthesis", "gap-report", "coverage-audit",
            "archived-memo", "sizing-object", "cutover",
            "priority-intent", "priority-ledger", "spike-result",
        }
        assert set(_TYPE_TO_GLOB) == expected

    def test_glob_values_match_oracle(self):
        assert _TYPE_TO_GLOB["bug"] == "state/bug-backlog/*.yaml"
        assert _TYPE_TO_GLOB["debt"] == "state/debt-backlog/*.yaml"
        assert _TYPE_TO_GLOB["improvement"] == "state/improvement-queue/*.yaml"
        assert _TYPE_TO_GLOB["tracker"] == "docs/project-tracker.md"
        assert _TYPE_TO_GLOB["roadmap"] == "state/roadmap/*/OVERVIEW.md"
        assert _TYPE_TO_GLOB["health-status"] == "state/health/*.md"
        assert _TYPE_TO_GLOB["decision-guide"] == "docs/guides/*-decisions.md"
        assert _TYPE_TO_GLOB["completion"] == "archive/completed/*/*.md"
        assert _TYPE_TO_GLOB["goal"] == "state/goals/*.yaml"
        assert _TYPE_TO_GLOB["research-synthesis"] == "docs/research/*.md"
        assert _TYPE_TO_GLOB["gap-report"] == "docs/research/*-gap-report.md"
        assert _TYPE_TO_GLOB["coverage-audit"] == "docs/research/*-coverage-audit.md"
        assert _TYPE_TO_GLOB["cutover"] == "state/roadmap/**/cutovers/*.md"


class TestGoalTypeCollectsAndParses:
    """``goal`` is `.yaml` whole-file frontmatter (no ``---`` fences), same
    shape as bug/debt/improvement/lesson — verified against an actual
    ``state/goals/*.yaml`` file at fix time."""

    def test_goal_yaml(self, tmp_path: Path):
        d = tmp_path / "state" / "goals"
        d.mkdir(parents=True)
        (d / "2026-07-10-example.yaml").write_text(
            dedent("""\
                schema: goal
                id: "goal-example"
                title: "example"
                status: active
                created: 2026-07-10
                """),
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "goal")
        assert len(files) == 1
        record = _load_record(files[0], tmp_path, "goal")
        assert record is not None
        assert record["frontmatter"]["id"] == "goal-example"
        assert record["frontmatter"]["status"] == "active"


class TestCutoverTypeCollectsAndParses:
    """``cutover`` is ``---``-delimited ``.md`` frontmatter (schema-shaped, not
    a ``.yaml`` whole-file type like goal/bug/debt) living at arbitrary depth
    under ``state/roadmap/**/cutovers/*.md`` — the depth is the whole point of
    wiring this type in (see ``_walk_glob_segments``'s general ``**`` support)."""

    def test_cutover_md_at_one_namespace_level(self, tmp_path: Path):
        d = tmp_path / "state" / "roadmap" / "lifecycle-vocab" / "cutovers"
        d.mkdir(parents=True)
        (d / "2026-07-25-example.md").write_text(
            dedent("""\
                ---
                surface: example surface
                phase: dual-write
                confirmed_consumers: []
                gate_source: {}
                ---
                Body.
                """),
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "cutover")
        assert len(files) == 1
        record = _load_record(files[0], tmp_path, "cutover")
        assert record is not None
        assert record["frontmatter"]["surface"] == "example surface"
        assert record["frontmatter"]["phase"] == "dual-write"


class TestPriorityIntentTypeCollectsAndParses:
    """``priority-intent`` is `.yaml` whole-file frontmatter (no ``---``
    fences), same shape as bug/debt/improvement/lesson/goal — verified
    against the shape ``priority.drain``'s producer-side contract
    (coordinator/schemas/priority-intent.schema.json) writes."""

    def test_priority_intent_yaml(self, tmp_path: Path):
        d = tmp_path / "state" / "priority-intent-inbox"
        d.mkdir(parents=True)
        (d / "0001-example.yaml").write_text(
            dedent("""\
                target_id: example-target
                priority: high
                requested_by: cockpit
                sequence: 1
                """),
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "priority-intent")
        assert len(files) == 1
        record = _load_record(files[0], tmp_path, "priority-intent")
        assert record is not None
        assert record["frontmatter"]["target_id"] == "example-target"
        assert record["frontmatter"]["priority"] == "high"


class TestPriorityLedgerTypeCollectsAndParses:
    """``priority-ledger`` is `.yaml` whole-file frontmatter, one file per
    target (filename-as-identity, no separate ``id`` field — see
    coordinator/schemas/priority-ledger.schema.json's NEGATIVE-SPEC (1))."""

    def test_priority_ledger_yaml(self, tmp_path: Path):
        d = tmp_path / "state" / "priority-ledger"
        d.mkdir(parents=True)
        (d / "example-target.yaml").write_text(
            dedent("""\
                target_id: example-target
                target_kind: plan
                priority: urgent
                source: op
                """),
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "priority-ledger")
        assert len(files) == 1
        record = _load_record(files[0], tmp_path, "priority-ledger")
        assert record is not None
        assert record["frontmatter"]["target_id"] == "example-target"
        assert record["frontmatter"]["priority"] == "urgent"


class TestTypeToGlobDerivedGate:
    """Derive-and-gate parity check: ``_TYPE_TO_GLOB`` (hand-maintained) vs.
    ``build_type_to_glob`` (schema-derived from example-doctrine-repo's ``coordinator/schemas/
    *.schema.json``). Fails loud when a record-shaped schema type is present
    in the derived registry but absent from BOTH ``_TYPE_TO_GLOB`` and the
    exclusion set below — this is exactly the shape of gap that let ``goal``
    go unqueryable until a sibling repo's goal-coverage-scan port reported
    false-empty results (see records_query.py module Negative-spec).

    Skips when the example-doctrine-repo checkout is unresolvable via the machine-local
    registry — same posture as ``test_schema_validate.py``'s ``_DOE_REPO``
    skip guard.
    """

    # Resolved via the canonical coordinator_core.testing.doe_root pointer-file
    # resolver, not a relative-sibling-checkout guess — see that module's
    # docstring for why a hardcoded parents[N]/"example-doctrine-repo" walk is retired
    # rather than mirrored here.
    _doe_root_str = resolve_doe_root()
    _DOE_REPO = Path(_doe_root_str) if _doe_root_str else None
    _SCHEMAS_DIR = _DOE_REPO / "coordinator" / "schemas" if _DOE_REPO is not None else None

    # Deliberately-excluded types — every member of the delta between
    # build_type_to_glob's schema-derived set and this module's _TYPE_TO_GLOB
    # (post-goal, post-research-synthesis/gap-report/coverage-audit,
    # post-archived-memo, post-sizing-object, post-cutover) NOT wired into
    # _TYPE_TO_GLOB.
    # Two categories, each type's reason inline:
    #
    #   (A) NOT a query-servable record collection at all — either a single
    #       fixed-path file (no wildcard: "query the record set" is meaningless
    #       for exactly one file) or a JSON file/glob that would hit this
    #       module's .md/.yaml frontmatter parser branches and silently
    #       collect zero records rather than parsing.
    #   (B) A genuine record-shaped collection (wildcard glob, .md or .yaml)
    #       that is simply not yet wired into _TYPE_TO_GLOB — out of scope
    #       for this fix, not structurally unqueryable. Candidates for a
    #       future add when a caller needs them.
    _TYPE_TO_GLOB_DELIBERATE_EXCLUSIONS: dict[str, str] = {
        # --- (A) not a record collection ---
        "capability-manifest":     "single JSON file (state/capabilities/manifest.json), no wildcard, not frontmatter-shaped",
        "fleet-capability-index":  "single JSON file (state/capabilities/fleet-index.json), no wildcard, not frontmatter-shaped",
        "docs-roadmap":            "single fixed file (docs/ROADMAP.md), no wildcard — not a record set",
        "health-ledger":           "single fixed file (state/health-ledger.md), no wildcard — not a record set",
        "orientation-cache":       "single fixed file (state/orientation_cache.md), no wildcard — not a record set",
        "pm-action-items":         "single fixed file (docs/PM-ACTION-ITEMS.md), no wildcard — not a record set",
        "product-changelog":       "single fixed file (docs/PRODUCT-CHANGELOG.md), no wildcard — not a record set",
        "repomap":                 "single fixed file (docs/repo-map.md), no wildcard — not a record set",
        "strategic-self-description": "single fixed file (state/strategic/self-description.yaml), no wildcard — not a record set",
        "review-integration-record": "JSON glob (state/review-trail/*-integration.json) — unparseable by this module's .md/.yaml branches",
        "review-trail":            "JSON glob (state/review-trail/*.json) — unparseable by this module's .md/.yaml branches",
        "session-hierarchy":       "JSON glob (state/session-hierarchy.*.json) — unparseable by this module's .md/.yaml branches",
        # --- (B) record-shaped, not yet wired (out of scope for this fix) ---
        "atlas-doc":               "record-shaped (docs/architecture/*.md) — not yet wired, out of scope for this fix",
        "atlas-system-doc":        "record-shaped (docs/architecture/systems/*.md) — not yet wired, out of scope for this fix",
        "audit-record":            "record-shaped (docs/architecture/audit-records/*.md) — not yet wired, out of scope for this fix",
        "cross-repo-commitment":   "record-shaped (state/cross-repo-commitments/*.yaml) — not yet wired, out of scope for this fix",
        "docs-check-sidecar":      "record-shaped (docs/plans/*.docs-check.md) — not yet wired, out of scope for this fix",
        "initiative":              "record-shaped (state/initiatives/*.yaml) — not yet wired, out of scope for this fix",
        "kr-suggestion":           "record-shaped (state/kr-suggestions/*.yaml) — reader (coordinator_core/goals/reassess_krs.py) globs this directory directly, mirroring its existing state/goals/*.yaml glob, rather than going through this query engine; not yet wired, out of scope for this fix",
        "integration-summary":     "record-shaped (docs/plans/*.integration-summary.md) — not yet wired, out of scope for this fix",
        "lessons-outbox":          "record-shaped (state/lessons-outbox/*.yaml) — not yet wired, out of scope for this fix",
        "plan-coverage-check":     "record-shaped (docs/plans/*.plan-coverage-check.md) — not yet wired, out of scope for this fix",
        "prior-art-check":         "record-shaped (docs/plans/*.prior-art-check.md) — not yet wired, out of scope for this fix",
        "problem-set":             "record-shaped (docs/problems/*.md) — not yet wired, out of scope for this fix",
        "review-findings":         "record-shaped (state/review-trail/findings/*.md) — not yet wired, out of scope for this fix",
        "review-residue-manifest": "record-shaped, yaml-frontmatter glob (**/skills/review/residue/*.md); every instance lives in example-doctrine-repo's coordinator/skills/review/residue/ tree, outside this repo's own worktree (0 on-disk in claude-klabauter) — same shape as the 'skill' exclusion below, not query-servable from this repo",
        "review-sidecar":          "record-shaped (docs/plans/*.review.md) — not yet wired, out of scope for this fix",
        "run-report":              "record-shaped, wildcard-dir glob (state/subagent-share/*/*.md) — not yet wired, out of scope for this fix",
        "skill":                   "record-shaped, wildcard-dir glob (plugins/coordinator-claude/coordinator/skills/*/SKILL.md); also lives outside this repo's own worktree (~/.claude plugin tree) — not yet wired, out of scope for this fix",
        "workstream":              "record-shaped (state/workstreams/*.yaml) — not yet wired, out of scope for this fix",
        "workstream-event":        "record-shaped (state/workstreams/events/*.yaml) — not yet wired, out of scope for this fix",
    }

    def _skip_if_unresolvable(self):
        if self._SCHEMAS_DIR is None or not self._SCHEMAS_DIR.is_dir():
            pytest.skip(
                f"example-doctrine-repo sibling schemas dir unresolvable at {self._SCHEMAS_DIR} "
                "— skipping derive-and-gate parity check (no live disk dependency "
                "for an ordinary run)."
            )

    def test_no_unwired_record_shaped_type(self):
        """A NEW record-shaped schema type landing upstream must appear in
        EITHER _TYPE_TO_GLOB OR the exclusion set above — never silently in
        neither, which is exactly how the ``goal`` gap reached this repo as
        a cross-repo memo instead of a test failure."""
        self._skip_if_unresolvable()
        from coordinator_core.frontmatter.schema_validate import build_type_to_glob

        derived = set(build_type_to_glob(self._SCHEMAS_DIR))
        unaccounted = derived - set(_TYPE_TO_GLOB) - set(self._TYPE_TO_GLOB_DELIBERATE_EXCLUSIONS)
        assert unaccounted == set(), (
            f"New schema-recognised type(s) {unaccounted} are neither wired into "
            "_TYPE_TO_GLOB nor named in _TYPE_TO_GLOB_DELIBERATE_EXCLUSIONS — "
            "classify each as query-servable (wire it) or not (add it to the "
            "exclusion set with a reason)."
        )

    def test_no_glob_disagreement_on_overlap(self):
        """Where both maps carry the same type, the glob string must agree —
        the two surfaces (hand-maintained + schema-derived) must never drift
        apart on a shared type."""
        self._skip_if_unresolvable()
        from coordinator_core.frontmatter.schema_validate import build_type_to_glob

        derived = build_type_to_glob(self._SCHEMAS_DIR)
        shared = set(derived) & set(_TYPE_TO_GLOB)
        disagreements = {
            t: (derived[t], _TYPE_TO_GLOB[t]) for t in shared if derived[t] != _TYPE_TO_GLOB[t]
        }
        assert disagreements == {}


class TestEachNewTypeCollectsAndParses:
    """Each of the 8 widened types collects its file(s) and parses frontmatter."""

    def test_bug_yaml(self, tmp_path: Path):
        d = tmp_path / "state" / "bug-backlog"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: Example bug\nstatus: open\nseverity: P2\n", encoding="utf-8",
        )
        files = _collect_files(tmp_path, "bug")
        assert len(files) == 1
        rec = _load_record(files[0], tmp_path, "bug")
        assert rec is not None
        assert rec["frontmatter"]["title"] == "Example bug"
        assert rec["frontmatter"]["liveness"] == "LIVE"

    def test_debt_yaml(self, tmp_path: Path):
        d = tmp_path / "state" / "debt-backlog"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: Example debt\nstatus: closed\n", encoding="utf-8",
        )
        files = _collect_files(tmp_path, "debt")
        rec = _load_record(files[0], tmp_path, "debt")
        assert rec["frontmatter"]["liveness"] == "DONE"

    def test_improvement_yaml(self, tmp_path: Path):
        d = tmp_path / "state" / "improvement-queue"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: Example improvement\nstatus: open\nproposed_action: do it\n",
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "improvement")
        rec = _load_record(files[0], tmp_path, "improvement")
        assert rec["frontmatter"]["proposed_action"] == "do it"

    def test_tracker_literal_filename(self, tmp_path: Path):
        d = tmp_path / "docs"
        d.mkdir(parents=True)
        (d / "project-tracker.md").write_text(
            "---\nstatus: active\n---\nTracker body.\n", encoding="utf-8",
        )
        # A sibling non-matching file must NOT be collected (literal-filename match).
        (d / "other.md").write_text("---\nstatus: active\n---\nOther.\n", encoding="utf-8")
        files = _collect_files(tmp_path, "tracker")
        assert [f.name for f in files] == ["project-tracker.md"]

    def test_health_status(self, tmp_path: Path):
        d = tmp_path / "state" / "health"
        d.mkdir(parents=True)
        (d / "engine.md").write_text("---\nstatus: active\n---\nBody.\n", encoding="utf-8")
        files = _collect_files(tmp_path, "health-status")
        rec = _load_record(files[0], tmp_path, "health-status")
        assert rec["frontmatter"]["liveness"] == "LIVE"

    def test_decision_guide_suffix_pattern(self, tmp_path: Path):
        d = tmp_path / "docs" / "guides"
        d.mkdir(parents=True)
        (d / "fifa-decisions.md").write_text(
            "---\nstatus: active\n---\nBody.\n", encoding="utf-8",
        )
        # A sibling .md file that does NOT match the *-decisions.md suffix is excluded.
        (d / "unrelated.md").write_text("---\nstatus: active\n---\nBody.\n", encoding="utf-8")
        files = _collect_files(tmp_path, "decision-guide")
        assert [f.name for f in files] == ["fifa-decisions.md"]

    def test_roadmap_wildcard_dir(self, tmp_path: Path):
        for name in ("claude-klabauter-roadmap", "rag-roadmap"):
            d = tmp_path / "state" / "roadmap" / name
            d.mkdir(parents=True)
            (d / "OVERVIEW.md").write_text(
                "---\nstatus: active\n---\nBody.\n", encoding="utf-8",
            )
        files = _collect_files(tmp_path, "roadmap")
        assert [f.parent.name for f in files] == ["claude-klabauter-roadmap", "rag-roadmap"]

    def test_completion_wildcard_dir(self, tmp_path: Path):
        for month in ("2026-06", "2026-07"):
            d = tmp_path / "archive" / "completed" / month
            d.mkdir(parents=True)
            (d / "entry.md").write_text(
                "---\nnature: fix\n---\nBody.\n", encoding="utf-8",
            )
        files = _collect_files(tmp_path, "completion")
        assert [f.parent.name for f in files] == ["2026-06", "2026-07"]

    def test_research_synthesis_sibling_exclusion(self, tmp_path: Path):
        """The regression this whole change hinges on: ``docs/research/*.md``
        is a SUPERSET glob shared with ``gap-report``/``coverage-audit`` —
        a ``--type research-synthesis`` query must return ONLY the plain
        synthesis file, never the co-located suffix-narrowed siblings."""
        d = tmp_path / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-22-widget-research.md").write_text(
            "---\ntitle: Widget Research\npipeline: web\n---\nBody.\n", encoding="utf-8",
        )
        (d / "2026-07-22-widget-gap-report.md").write_text(
            "---\ngap_count: 3\n---\nBody.\n", encoding="utf-8",
        )
        (d / "2026-07-22-widget-coverage-audit.md").write_text(
            "---\npresent_count: 5\n---\nBody.\n", encoding="utf-8",
        )
        files = _collect_files(tmp_path, "research-synthesis")
        assert [f.name for f in files] == ["2026-07-22-widget-research.md"]

    def test_gap_report_collects_its_own_file(self, tmp_path: Path):
        d = tmp_path / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-22-widget-research.md").write_text(
            "---\ntitle: Widget Research\n---\nBody.\n", encoding="utf-8",
        )
        (d / "2026-07-22-widget-gap-report.md").write_text(
            "---\ngap_count: 3\ncoverage_score: 0.6\ndeepening_recommended: true\n---\nBody.\n",
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "gap-report")
        assert [f.name for f in files] == ["2026-07-22-widget-gap-report.md"]
        rec = _load_record(files[0], tmp_path, "gap-report")
        assert rec["frontmatter"]["gap_count"] == 3

    def test_coverage_audit_collects_its_own_file(self, tmp_path: Path):
        d = tmp_path / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-22-widget-research.md").write_text(
            "---\ntitle: Widget Research\n---\nBody.\n", encoding="utf-8",
        )
        (d / "2026-07-22-widget-coverage-audit.md").write_text(
            "---\npresent_count: 5\nabsent_count: 2\n---\nBody.\n", encoding="utf-8",
        )
        files = _collect_files(tmp_path, "coverage-audit")
        assert [f.name for f in files] == ["2026-07-22-widget-coverage-audit.md"]
        rec = _load_record(files[0], tmp_path, "coverage-audit")
        assert rec["frontmatter"]["present_count"] == 5

    def test_archived_memo_collects_and_reports_done_liveness(self, tmp_path: Path):
        """Review: code-reviewer (F2) — regression net for F1: an
        ``archived-memo`` record whose status is memo-vocabulary
        ("actioned", not in handoff's `_TERMINAL_STATUS`) must still resolve
        to DONE liveness, since every file under cross-repo/archive/ is
        unconditionally terminal by directory placement. Fails red against
        pre-F1 code (falls through to the graceful default and reports LIVE)."""
        d = tmp_path / "cross-repo" / "archive"
        d.mkdir(parents=True)
        (d / "2026-07-24-example-memo.md").write_text(
            "---\nstatus: actioned\n---\nBody.\n", encoding="utf-8",
        )
        files = _collect_files(tmp_path, "archived-memo")
        assert [f.name for f in files] == ["2026-07-24-example-memo.md"]
        rec = _load_record(files[0], tmp_path, "archived-memo")
        assert rec is not None
        assert rec["frontmatter"]["liveness"] == "DONE"


class TestNewTypeMarkdownListFormatEndToEnd:
    """`_handler`'s markdown-list format (the DEFAULT when `format` is
    omitted) for the 3 2026-07-22-widened types — Review: code-reviewer F1.

    `TestNewTypeMarkdownListRendering` (below) only unit-tests the renderer
    functions directly; `TestEachNewTypeCollectsAndParses` (above) only
    exercises collection/parsing. Neither calls `_handler` end-to-end, so
    neither would have caught `_TYPE_DISPLAY`'s missing entries for these 3
    types — the bare-path default fallback still produces a plausible-looking
    markdown line, just without the oracle's dedicated columns, so a
    collection/parsing-only test suite cannot distinguish "rendered
    correctly" from "silently fell back."
    """

    def test_research_synthesis_default_format_uses_dedicated_renderer(self, tmp_path: Path):
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        d = worktree / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-22-widget-research.md").write_text(
            "---\ntitle: Widget Research\npipeline: web\ncoverage_score: 0.6\n---\nBody.\n",
            encoding="utf-8",
        )
        result = _run(_handler(params={"type": "research-synthesis"}, repo_root=git_dir))
        assert result["records"] == (
            "- [Widget Research](docs/research/2026-07-22-widget-research.md) — "
            "pipeline: web, score: 0.6"
        )

    def test_gap_report_default_format_uses_dedicated_renderer(self, tmp_path: Path):
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        d = worktree / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-22-widget-gap-report.md").write_text(
            "---\ngap_count: 3\ncoverage_score: 0.6\ndeepening_recommended: true\n---\nBody.\n",
            encoding="utf-8",
        )
        result = _run(_handler(params={"type": "gap-report"}, repo_root=git_dir))
        assert result["records"] == (
            "- [2026-07-22-widget-gap-report.md](docs/research/2026-07-22-widget-gap-report.md)"
            " — gaps: 3, score: 0.6, deepening: true"
        )

    def test_coverage_audit_default_format_uses_dedicated_renderer(self, tmp_path: Path):
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        d = worktree / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-22-widget-coverage-audit.md").write_text(
            "---\npresent_count: 5\nabsent_count: 2\n---\nBody.\n", encoding="utf-8",
        )
        result = _run(_handler(params={"type": "coverage-audit"}, repo_root=git_dir))
        assert result["records"] == (
            "- [2026-07-22-widget-coverage-audit.md](docs/research/2026-07-22-widget-coverage-audit.md)"
            " — present: 5, absent: 2"
        )


class TestSiblingExclusionDerivedFromWiredSet:
    """Derive-and-gate check pinning the equivalence claim in
    ``_apply_sibling_exclusion``'s docstring: the map-local filter (derived
    only from ``_TYPE_TO_GLOB``) is sound ONLY as long as no UNWIRED example-doctrine-repo
    schema glob is a more-specific sibling of a WIRED glob under the same
    directory — that is exactly the condition under which this op's filter
    would silently diverge from the oracle's all-schemas filter. Fails loud
    the moment a new example-doctrine-repo schema breaks that equivalence.

    Skips when the example-doctrine-repo checkout is unresolvable via the machine-local
    registry — same posture as ``TestTypeToGlobDerivedGate``.
    """

    _doe_root_str = resolve_doe_root()
    _DOE_REPO = Path(_doe_root_str) if _doe_root_str else None
    _SCHEMAS_DIR = _DOE_REPO / "coordinator" / "schemas" if _DOE_REPO is not None else None

    # 'plan' is exempted: its docs/plans/*.md glob DOES have genuine unwired
    # suffix-sidecar siblings (docs-check-sidecar/integration-summary/
    # plan-coverage-check/prior-art-check/review-sidecar), but those are
    # already excluded by the SEPARATE, dedicated `_apply_plan_filename_filter`
    # positive-allowlist — the same "both filters coexist" architecture the
    # oracle itself uses (query-records.js's own comment at bin/query-records.js
    # :1325-1326). No divergence risk: the sidecar files never reach
    # `_apply_sibling_exclusion` matched into a plan result either way.
    _EXEMPT_WIRED_TYPES: frozenset[str] = frozenset({'plan'})

    def _skip_if_unresolvable(self):
        if self._SCHEMAS_DIR is None or not self._SCHEMAS_DIR.is_dir():
            pytest.skip(
                f"example-doctrine-repo sibling schemas dir unresolvable at {self._SCHEMAS_DIR} "
                "— skipping derive-and-gate sibling-exclusion check (no live disk "
                "dependency for an ordinary run)."
            )

    @staticmethod
    def _sample_for_filename_glob(name_pattern: str) -> str:
        """Concrete string satisfying ``name_pattern`` — substitutes each
        wildcard with a fixed placeholder so pattern-overlap can be checked by
        regex match rather than by string length/wildcard-count alone (which
        would false-positive on two disjoint LITERAL filenames merely sharing
        a directory — e.g. ``project-tracker.md`` vs ``PRODUCT-CHANGELOG.md``)."""
        return name_pattern.replace('*', 'X').replace('?', 'Y')

    def test_no_unwired_schema_is_a_more_specific_sibling(self):
        from coordinator_core.frontmatter.schema_validate import build_type_to_glob
        from coordinator_core.ops.records_query import (
            _SIBLING_EXCLUSION_INELIGIBLE,
            _segment_to_regex,
            _specificity_key,
        )

        self._skip_if_unresolvable()
        derived = build_type_to_glob(self._SCHEMAS_DIR)
        unwired = {t: g for t, g in derived.items() if t not in _TYPE_TO_GLOB}

        violations: list[str] = []
        for wired_type, wired_glob in _TYPE_TO_GLOB.items():
            if wired_type in _SIBLING_EXCLUSION_INELIGIBLE:
                continue
            if wired_type in self._EXEMPT_WIRED_TYPES:
                continue
            wired_dir = Path(wired_glob).parent
            wired_key = _specificity_key(wired_glob)
            wired_re = _segment_to_regex(Path(wired_glob).name)
            for unwired_type, unwired_glob in unwired.items():
                if Path(unwired_glob).parent != wired_dir:
                    continue
                if _specificity_key(unwired_glob) >= wired_key:
                    continue
                sample = self._sample_for_filename_glob(Path(unwired_glob).name)
                if not wired_re.match(sample):
                    continue  # more "specific" by string-length alone, but no actual overlap
                violations.append(
                    f"unwired {unwired_type!r} ({unwired_glob!r}) is a more "
                    f"specific sibling of wired {wired_type!r} ({wired_glob!r}) and "
                    "genuinely overlaps it — the map-local sibling-exclusion filter "
                    "would diverge from the oracle's all-schemas filter until this "
                    "type is wired or reclassified."
                )
        assert violations == [], "\n".join(violations)


class TestSiblingExclusionTieBreak:
    """Direct unit coverage for `_apply_sibling_exclusion`'s tie-break branch
    (`sib_key < best_key`, never `<=`) — Review: code-reviewer F2.

    No currently-wired ELIGIBLE sibling pair shares an equal
    `_specificity_key` (research-synthesis/gap-report/coverage-audit's globs
    all differ in length), so neither this test's real-data sibling (via
    `TestNewTypeGlobCoverage`) nor `TestSiblingExclusionDerivedFromWiredSet`'s
    derive-and-gate check ever exercises the tie branch itself — both compare
    unwired-vs-wired or genuinely-different specificities. Two synthetic
    equal-specificity fixture types, monkeypatched onto `_TYPE_TO_GLOB`,
    close that gap directly.
    """

    def test_equal_specificity_sibling_does_not_spuriously_exclude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import coordinator_core.ops.records_query as rq

        fixture_map = dict(rq._TYPE_TO_GLOB)
        fixture_map['fixture-a'] = 'fixturedir/*.md'
        fixture_map['fixture-b'] = 'fixturedir/*.md'
        monkeypatch.setattr(rq, '_TYPE_TO_GLOB', fixture_map)

        base_dir = tmp_path / 'fixturedir'
        base_dir.mkdir()
        shared_file = base_dir / 'shared.md'
        shared_file.write_text('x', encoding='utf-8')

        # Same file, same specificity_key on both sides — queried from EITHER
        # type's perspective, the tie must resolve in the QUERIED type's favor
        # (kept), never spuriously excluded by its equal-specificity sibling.
        assert rq._apply_sibling_exclusion([shared_file], 'fixture-a', base_dir) == [shared_file]
        assert rq._apply_sibling_exclusion([shared_file], 'fixture-b', base_dir) == [shared_file]


class TestWildcardDirOrderingParity:
    """_walk_glob_segments enumerates wildcard-dir levels in scandir-alphasort order."""

    def test_alpha_sorted_across_unsorted_creation_order(self, tmp_path: Path):
        # Create directories out of alphabetical order to prove sorting, not
        # creation-order or filesystem-native order, drives the result.
        for name in ("zzz-roadmap", "aaa-roadmap", "mmm-roadmap"):
            d = tmp_path / "state" / "roadmap" / name
            d.mkdir(parents=True)
            (d / "OVERVIEW.md").write_text("---\nstatus: active\n---\nBody.\n", encoding="utf-8")
        results = _walk_glob_segments(tmp_path, "state/roadmap/*/OVERVIEW.md".split("/"))
        assert [p.parent.name for p in results] == ["aaa-roadmap", "mmm-roadmap", "zzz-roadmap"]

    def test_missing_base_dir_returns_empty(self, tmp_path: Path):
        assert _walk_glob_segments(tmp_path, "state/roadmap/*/OVERVIEW.md".split("/")) == []


class TestArbitraryDepthDoubleStarGlob:
    """``**`` in ``_walk_glob_segments`` matches ZERO-OR-MORE directory levels —
    a one-level approximation (treating ``**`` as a plain ``*``) would pass a
    single-level fixture but silently under-collect a genuinely nested one, the
    same false-empty shape the ``goal`` gap produced. These tests exercise
    more than one level so a broken one-level-only implementation fails loud."""

    _SEGMENTS = "state/roadmap/**/cutovers/*.md".split("/")

    def test_collects_across_more_than_one_directory_level(self, tmp_path: Path):
        one_level = tmp_path / "state" / "roadmap" / "ns" / "cutovers"
        two_level = tmp_path / "state" / "roadmap" / "ns" / "sub" / "cutovers"
        one_level.mkdir(parents=True)
        two_level.mkdir(parents=True)
        (one_level / "a.md").write_text("---\nstatus: open\n---\nBody.\n", encoding="utf-8")
        (two_level / "b.md").write_text("---\nstatus: open\n---\nBody.\n", encoding="utf-8")

        results = _walk_glob_segments(tmp_path, self._SEGMENTS)
        names = sorted(str(p.relative_to(tmp_path)) for p in results)
        assert names == [
            "state/roadmap/ns/cutovers/a.md",
            "state/roadmap/ns/sub/cutovers/b.md",
        ]

    def test_zero_level_matches_directly_under_anchor(self, tmp_path: Path):
        # `**` also admits ZERO intervening directories — a cutovers/ dir
        # living directly under state/roadmap/ (no namespace segment at all).
        d = tmp_path / "state" / "roadmap" / "cutovers"
        d.mkdir(parents=True)
        (d / "c.md").write_text("---\nstatus: open\n---\nBody.\n", encoding="utf-8")

        results = _walk_glob_segments(tmp_path, self._SEGMENTS)
        names = sorted(str(p.relative_to(tmp_path)) for p in results)
        assert names == ["state/roadmap/cutovers/c.md"]

    def test_zero_and_multi_level_both_collected_together(self, tmp_path: Path):
        zero = tmp_path / "state" / "roadmap" / "cutovers"
        nested = tmp_path / "state" / "roadmap" / "a" / "b" / "c" / "cutovers"
        zero.mkdir(parents=True)
        nested.mkdir(parents=True)
        (zero / "top.md").write_text("---\nstatus: open\n---\nBody.\n", encoding="utf-8")
        (nested / "deep.md").write_text("---\nstatus: open\n---\nBody.\n", encoding="utf-8")

        results = _walk_glob_segments(tmp_path, self._SEGMENTS)
        names = sorted(str(p.relative_to(tmp_path)) for p in results)
        assert names == [
            "state/roadmap/a/b/c/cutovers/deep.md",
            "state/roadmap/cutovers/top.md",
        ]


class TestRoadmapStatusNormalization:
    """_normalize_roadmap_status ports query-records.js's normalizeRoadmapStatus 1:1."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("final-approved", "active"),
            ("approved", "active"),
            ("draft", "planning"),
            ("in-review", "planning"),
            ("planning", "planning"),
            ("active", "active"),
            ("blocked", "blocked"),
            ("shipped", "shipped"),
            ("archived", "archived"),
            ("some-unmapped-value", "active"),
        ],
    )
    def test_status_mapping(self, raw, expected):
        fm = {"status": raw}
        _normalize_roadmap_status(fm, "roadmap")
        assert fm["status"] == expected

    def test_noop_for_non_roadmap_type(self):
        fm = {"status": "draft"}
        _normalize_roadmap_status(fm, "plan")
        assert fm["status"] == "draft"  # untouched — draft is not a plan enum value either

    def test_noop_when_status_absent(self):
        fm = {}
        _normalize_roadmap_status(fm, "roadmap")
        assert "status" not in fm

    def test_applied_before_liveness_via_load_record(self, tmp_path: Path):
        d = tmp_path / "state" / "roadmap" / "claude-klabauter-roadmap"
        d.mkdir(parents=True)
        f = d / "OVERVIEW.md"
        f.write_text("---\nstatus: final-approved\n---\nBody.\n", encoding="utf-8")
        rec = _load_record(f, tmp_path, "roadmap")
        assert rec["frontmatter"]["status"] == "active"
        assert rec["frontmatter"]["liveness"] == "LIVE"


class TestSinceFilteringNewTypes:
    """--since composes with the widened type set (created>=cutoff, records lacking
    created excluded) — same ordering/semantics as the pre-existing handoff coverage,
    exercised here against a .yaml-backed type."""

    def test_since_excludes_older_bug_and_missing_created(self, tmp_path: Path):
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)
        d = worktree / "state" / "bug-backlog"
        d.mkdir(parents=True)
        (d / "2020-01-01-old.yaml").write_text(
            "title: Old bug\nstatus: open\ncreated: 2020-01-01\n", encoding="utf-8",
        )
        (d / "2026-07-01-recent.yaml").write_text(
            "title: Recent bug\nstatus: open\ncreated: 2026-07-01\n", encoding="utf-8",
        )
        (d / "2026-07-02-no-created.yaml").write_text(
            "title: No created field\nstatus: open\n", encoding="utf-8",
        )
        result = _run(
            _handler(
                params={"type": "bug", "since": "2026-01-01", "format": "json"},
                repo_root=git_dir,
            )
        )
        titles = {r["frontmatter"]["title"] for r in result["records"]}
        assert titles == {"Recent bug"}


# ---------------------------------------------------------------------------
# Node-parity regression fixtures — three constructs a strict yaml.safe_load /
# dag._read_meta parse diverged on vs. query-records.js's lenient _parseYaml
# (freeze-query-records-grammar.md parity harness, 2026-07-22).
# ---------------------------------------------------------------------------


class TestWholeFileYamlLenientParse:
    """.yaml whole-file records parse through the byte-parity ``parse_yaml`` port
    (coordinator_core.frontmatter.schema_validate), not strict ``yaml.safe_load`` —
    query-records.js's ``_parseYaml`` accepts real on-disk shapes PyYAML rejects."""

    def test_backtick_leading_title_accepted(self, tmp_path: Path):
        # Mirrors state/debt-backlog/2026-06-15-agent-install.yaml: an unquoted
        # scalar value that begins with a backtick — a hard PyYAML parse error
        # ("found character '`' that cannot start any token"), but a plain
        # string under the lenient line-oriented parser.
        d = tmp_path / "state" / "debt-backlog"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: `agent-install\nstatus: open\n", encoding="utf-8",
        )
        files = _collect_files(tmp_path, "debt")
        rec = _load_record(files[0], tmp_path, "debt")
        assert rec is not None
        assert rec["frontmatter"]["title"] == "`agent-install"

    def test_unquoted_mid_line_colon_value_accepted(self, tmp_path: Path):
        # Mirrors state/bug-backlog/2026-07-14-token-fchmod-and-liveness-bash-
        # dep-win32.yaml: an unquoted scalar value containing ": " mid-line —
        # PyYAML raises "mapping values are not allowed here" (it re-parses the
        # embedded ": " as a nested key:value), but the line-oriented parser
        # takes everything after the FIRST top-level colon as the scalar value.
        d = tmp_path / "state" / "bug-backlog"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: liveness.py bash-shell dependency (was: token.py fixed)\n"
            "status: open\n",
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "bug")
        rec = _load_record(files[0], tmp_path, "bug")
        assert rec is not None
        assert (
            rec["frontmatter"]["title"]
            == "liveness.py bash-shell dependency (was: token.py fixed)"
        )


class TestMdFrontmatterScalarAndListParity:
    """.md delimited-frontmatter parse also goes through the byte-parity
    ``parse_frontmatter``/``parse_yaml`` port — NOT ``coordinator_core.dag
    ._read_meta`` (a separate hand-rolled parser that had drifted from the
    schema.js oracle on these two constructs)."""

    def test_scientific_notation_looking_scalar_stays_string(self, tmp_path: Path):
        # Mirrors archive/completed/.../...-7b374f.md's `commits: ['9e015366', ...]`
        # — a bare list item that LOOKS like scientific notation (digit-e-digit)
        # must stay the literal string '9e015366', not overflow to float('inf').
        d = tmp_path / "archive" / "completed" / "2026-07"
        d.mkdir(parents=True)
        (d / "entry.md").write_text(
            "---\n"
            "commits:\n"
            "  - 9e015366\n"
            "  - 8ed8906c\n"
            "---\n"
            "Body.\n",
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "completion")
        rec = _load_record(files[0], tmp_path, "completion")
        assert rec is not None
        assert rec["frontmatter"]["commits"] == ["9e015366", "8ed8906c"]

    def test_list_item_nested_mapping(self, tmp_path: Path):
        # Mirrors docs/plans/2026-07-14-claude-klabauter-windows-portability.md's
        # `related:` list, whose second entry is `- memory: <value>` — a
        # single-key nested mapping, not a flat scalar string.
        d = tmp_path / "docs" / "plans"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.md").write_text(
            "---\n"
            "related:\n"
            "  - state/bug-backlog/2026-07-06-example.yaml\n"
            "  - memory: coordinator-core-windows-blockers, coordinator-root-read-surface-principle\n"
            "---\n"
            "Body.\n",
            encoding="utf-8",
        )
        files = _apply_plan_filename_filter(_collect_files(tmp_path, "plan"))
        rec = _load_record(files[0], tmp_path, "plan")
        assert rec is not None
        related = rec["frontmatter"]["related"]
        assert related[0] == "state/bug-backlog/2026-07-06-example.yaml"
        assert related[1] == {
            "memory": "coordinator-core-windows-blockers, coordinator-root-read-surface-principle"
        }


# ---------------------------------------------------------------------------
# Wave1: decision / review / lesson / handoff-ledger / research-claim —
# the 5 record types outstanding after T4d-g1c. Spec backlink:
# cross-repo memo 2026-07-16-claude-central-em-records-query-surface-gaps.
# ---------------------------------------------------------------------------


class TestDecisionType:
    """decision: static glob (docs/decisions/*.md), ordinary .md frontmatter parse."""

    def test_collects_and_parses(self, tmp_path: Path):
        d = tmp_path / "docs" / "decisions"
        d.mkdir(parents=True)
        (d / "2026-07-01-example.md").write_text(
            "---\ntitle: Example Decision\nstatus: accepted\n---\nBody.\n",
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "decision")
        assert len(files) == 1
        rec = _load_record(files[0], tmp_path, "decision")
        assert rec is not None
        assert rec["frontmatter"]["title"] == "Example Decision"
        # decision liveness single-axis rule: accepted -> DONE.
        assert rec["frontmatter"]["liveness"] == "DONE"

    def test_absent_directory_yields_empty(self, tmp_path: Path):
        assert _collect_files(tmp_path, "decision") == []


class TestReviewType:
    """review: static glob (state/reviews/*.md), ordinary .md frontmatter parse."""

    def test_collects_and_parses(self, tmp_path: Path):
        d = tmp_path / "state" / "reviews"
        d.mkdir(parents=True)
        (d / "2026-07-01-example.md").write_text(
            "---\ntitle: Example Review\nreviewer: the Staff Engineer\nfindings_count: 3\n---\nBody.\n",
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "review")
        assert len(files) == 1
        rec = _load_record(files[0], tmp_path, "review")
        assert rec is not None
        assert rec["frontmatter"]["reviewer"] == "the Staff Engineer"
        # review has no dedicated liveness() branch — graceful default applies.
        assert rec["frontmatter"]["liveness"] == "LIVE"

    def test_absent_directory_yields_empty(self, tmp_path: Path):
        assert _collect_files(tmp_path, "review") == []


class TestLessonType:
    """lesson: static glob (state/lessons/*.yaml), whole-file YAML frontmatter parse."""

    def test_collects_and_parses(self, tmp_path: Path):
        d = tmp_path / "state" / "lessons"
        d.mkdir(parents=True)
        (d / "2026-07-01-example.yaml").write_text(
            "title: Example Lesson\ntier: universal\nstatus: applied\n",
            encoding="utf-8",
        )
        files = _collect_files(tmp_path, "lesson")
        assert len(files) == 1
        rec = _load_record(files[0], tmp_path, "lesson")
        assert rec is not None
        assert rec["frontmatter"]["tier"] == "universal"
        assert rec["frontmatter"]["liveness"] == "DONE"

    def test_absent_directory_yields_empty(self, tmp_path: Path):
        assert _collect_files(tmp_path, "lesson") == []


class TestHandoffLedgerParsing:
    """_parse_handoff_ledger_blocks — byte-exact port of parseHandoffLedger's
    state machine (bin/query-records.js:1154-1236)."""

    def test_single_block(self):
        content = (
            "# A handoff\n\n"
            "## Session Ledger\n\n"
            "| Field            | Value      |\n"
            "|-------------------|------------|\n"
            "| agent_dispatches | 26         |\n"
            "| opus_dispatches  | 4          |\n"
            "| em_tokens        | 482,000    |\n"
            "| tshirt           | L          |\n"
            "| session_id       | sid-1      |\n"
            "| created          | 2026-05-19 |\n\n"
            "Some trailing prose.\n"
        )
        blocks = _parse_handoff_ledger_blocks(content)
        assert len(blocks) == 1
        fields = blocks[0]
        assert fields["agent_dispatches"] == "26"
        assert fields["opus_dispatches"] == "4"
        # Commas stripped so numeric compare/sort works.
        assert fields["em_tokens"] == "482000"
        assert fields["tshirt"] == "L"
        assert fields["session_id"] == "sid-1"
        assert fields["created"] == "2026-05-19"

    def test_multiple_blocks_in_one_file(self):
        content = (
            "## Session Ledger\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| tshirt | S |\n\n"
            "## Session Ledger\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| tshirt | M |\n"
        )
        blocks = _parse_handoff_ledger_blocks(content)
        assert [b["tshirt"] for b in blocks] == ["S", "M"]

    def test_no_ledger_heading_yields_empty(self):
        assert _parse_handoff_ledger_blocks("# Just a handoff\n\nNo table here.\n") == []

    def test_block_ends_at_next_heading_without_blank_line(self):
        content = (
            "## Session Ledger\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| tshirt | S |\n"
            "## Next Section\n"
            "More prose.\n"
        )
        blocks = _parse_handoff_ledger_blocks(content)
        assert len(blocks) == 1
        assert blocks[0]["tshirt"] == "S"


class TestHandoffLedgerCollection:
    """_collect_handoff_ledger_records — synthetic N-records-per-file collection,
    crawling BOTH live and archived handoffs (live first, then archive)."""

    def test_live_and_archive_both_crawled_with_fragment_paths(self, tmp_path: Path):
        live_dir = tmp_path / "state" / "handoffs"
        live_dir.mkdir(parents=True)
        (live_dir / "live-hoff.md").write_text(
            "## Session Ledger\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| tshirt | L |\n"
            "| session_id | live-sid |\n",
            encoding="utf-8",
        )
        archive_dir = tmp_path / "archive" / "handoffs" / "2026-06"
        archive_dir.mkdir(parents=True)
        (archive_dir / "archived-hoff.md").write_text(
            "## Session Ledger\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| tshirt | S |\n"
            "| session_id | archived-sid |\n",
            encoding="utf-8",
        )
        records = _collect_handoff_ledger_records(tmp_path)
        paths = [r["path"] for r in records]
        assert paths == [
            "state/handoffs/live-hoff.md#ledger-0",
            "archive/handoffs/2026-06/archived-hoff.md#ledger-0",
        ]
        assert records[0]["frontmatter"]["session_id"] == "live-sid"
        assert records[1]["frontmatter"]["session_id"] == "archived-sid"
        # Graceful-default liveness — no status field on a ledger record.
        assert records[0]["frontmatter"]["liveness"] == "LIVE"

    def test_multiple_ledger_blocks_get_distinct_fragment_indices(self, tmp_path: Path):
        live_dir = tmp_path / "state" / "handoffs"
        live_dir.mkdir(parents=True)
        (live_dir / "multi.md").write_text(
            "## Session Ledger\n\n| Field | Value |\n|---|---|\n| tshirt | S |\n\n"
            "## Session Ledger\n\n| Field | Value |\n|---|---|\n| tshirt | M |\n",
            encoding="utf-8",
        )
        records = _collect_handoff_ledger_records(tmp_path)
        assert [r["path"] for r in records] == [
            "state/handoffs/multi.md#ledger-0",
            "state/handoffs/multi.md#ledger-1",
        ]

    def test_absent_directories_yield_empty(self, tmp_path: Path):
        assert _collect_handoff_ledger_records(tmp_path) == []


class TestResearchClaimCollection:
    """_collect_research_claim_records — synthetic N-records-per-file collection
    over docs/research/*.claims.json array elements."""

    def test_collects_one_record_per_array_element(self, tmp_path: Path):
        import json as _json

        d = tmp_path / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-01-example.claims.json").write_text(
            _json.dumps([
                {"claim_text": "First claim", "confidence": "high", "type": "empirical"},
                {"claim_text": "Second claim", "confidence": "low", "type": "inference"},
            ]),
            encoding="utf-8",
        )
        records = _collect_research_claim_records(tmp_path)
        assert [r["path"] for r in records] == [
            "docs/research/2026-07-01-example.claims.json#claim-0",
            "docs/research/2026-07-01-example.claims.json#claim-1",
        ]
        assert records[0]["frontmatter"]["claim_text"] == "First claim"
        assert records[1]["frontmatter"]["confidence"] == "low"
        # Graceful-default liveness — no status field on a claim record.
        assert records[0]["frontmatter"]["liveness"] == "LIVE"

    def test_non_array_top_level_is_skipped(self, tmp_path: Path):
        d = tmp_path / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-01-not-an-array.claims.json").write_text(
            '{"claim_text": "not in an array"}', encoding="utf-8",
        )
        assert _collect_research_claim_records(tmp_path) == []

    def test_unparseable_json_is_skipped_and_warned(self, tmp_path: Path, capsys):
        d = tmp_path / "docs" / "research"
        d.mkdir(parents=True)
        (d / "2026-07-01-broken.claims.json").write_text(
            "{not valid json", encoding="utf-8",
        )
        assert _collect_research_claim_records(tmp_path) == []
        captured = capsys.readouterr()
        assert "2026-07-01-broken.claims.json" in captured.err

    def test_absent_directory_yields_empty(self, tmp_path: Path):
        assert _collect_research_claim_records(tmp_path) == []


class TestNewTypeMarkdownListRendering:
    """_TYPE_DISPLAY renderers for the 5 new types — including the ``??``-vs-``||``
    zero-value cases (findings_count / agent_dispatches / opus_dispatches)."""

    def test_decision_render(self):
        line = _TYPE_DISPLAY["decision"]("docs/decisions/x.md", {"title": "X", "status": "accepted"})
        assert line == "- [X](docs/decisions/x.md) — accepted"

    def test_review_render_with_findings_count(self):
        line = _TYPE_DISPLAY["review"](
            "state/reviews/x.md", {"title": "X", "reviewer": "the Staff Engineer", "findings_count": 3},
        )
        assert line == "- [X](state/reviews/x.md) — reviewer: the Staff Engineer, findings: 3"

    def test_review_render_zero_findings_count_is_not_question_mark(self):
        """`??`, not `||` — findings_count=0 must render as 0, not '?'."""
        line = _TYPE_DISPLAY["review"](
            "state/reviews/x.md", {"title": "X", "reviewer": "the Staff Engineer", "findings_count": 0},
        )
        assert "findings: 0" in line
        assert "findings: ?" not in line

    def test_review_render_missing_findings_count_is_question_mark(self):
        line = _TYPE_DISPLAY["review"]("state/reviews/x.md", {"title": "X"})
        assert "findings: ?" in line

    def test_lesson_render(self):
        line = _TYPE_DISPLAY["lesson"]("state/lessons/x.yaml", {"title": "X", "tier": "universal"})
        assert line == "- **X** [universal]"

    def test_handoff_ledger_render_zero_dispatch_counts_are_not_question_marks(self):
        """`??` sites — agent_dispatches/opus_dispatches=0 must render as 0, not '?'."""
        line = _TYPE_DISPLAY["handoff-ledger"](
            "state/handoffs/x.md#ledger-0",
            {"tshirt": "S", "agent_dispatches": 0, "opus_dispatches": 0, "session_id": "sid"},
        )
        assert "agents=0" in line
        assert "opus=0" in line
        assert "agents=?" not in line
        assert "opus=?" not in line

    def test_handoff_ledger_render_missing_dispatch_counts_are_question_marks(self):
        line = _TYPE_DISPLAY["handoff-ledger"]("state/handoffs/x.md#ledger-0", {})
        assert "agents=?" in line
        assert "opus=?" in line
        assert "tshirt=?" in line

    def test_research_claim_render(self):
        line = _TYPE_DISPLAY["research-claim"](
            "docs/research/x.claims.json#claim-0",
            {"claim_text": "A claim", "confidence": "high", "type": "empirical"},
        )
        assert line == "- A claim [high] (empirical)"


class TestUnattachedUnionLens:
    """``unattached=true`` with no ``type`` — the multi-type null-initiative-FK
    union lens (``_query_unattached_all`` / ``UNATTACHED_TYPES``), port of
    query-records.js's ``queryUnattachedAll``.

    Spec backlink: docs/plans/2026-07-04-initiative-govern-sweep-prioritize-doe-d.md § C3 (AC4)
    """

    @pytest.fixture()
    def unattached_repo(self, tmp_path: Path):
        """One attached + one unattached record in each UNATTACHED_TYPES member,
        plus a non-member type (decision) that must never appear in the union.
        """
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)

        bug_dir = worktree / "state" / "bug-backlog"
        bug_dir.mkdir(parents=True)
        (bug_dir / "2026-07-01-attached.yaml").write_text(
            "title: Attached bug\nstatus: open\ninitiative: init-x\ncreated: 2026-07-01\n",
            encoding="utf-8",
        )
        (bug_dir / "2026-07-02-unattached.yaml").write_text(
            "title: Unattached bug\nstatus: open\ncreated: 2026-07-02\n",
            encoding="utf-8",
        )

        debt_dir = worktree / "state" / "debt-backlog"
        debt_dir.mkdir(parents=True)
        (debt_dir / "2026-07-01-attached.yaml").write_text(
            "title: Attached debt\nstatus: open\ninitiative: init-x\ncreated: 2026-07-01\n",
            encoding="utf-8",
        )
        (debt_dir / "2026-07-02-unattached.yaml").write_text(
            "title: Unattached debt\nstatus: open\ncreated: 2026-07-02\n",
            encoding="utf-8",
        )

        imp_dir = worktree / "state" / "improvement-queue"
        imp_dir.mkdir(parents=True)
        (imp_dir / "2026-07-01-attached.yaml").write_text(
            "title: Attached improvement\nstatus: open\ninitiative: init-x\n"
            "proposed_action: x\ncreated: 2026-07-01\n",
            encoding="utf-8",
        )
        (imp_dir / "2026-07-02-unattached.yaml").write_text(
            "title: Unattached improvement\nstatus: open\nproposed_action: y\n"
            "created: 2026-07-02\n",
            encoding="utf-8",
        )

        roadmap_dir = worktree / "state" / "roadmap"
        (roadmap_dir / "attached-roadmap").mkdir(parents=True)
        (roadmap_dir / "attached-roadmap" / "OVERVIEW.md").write_text(
            "---\nstatus: active\ninitiative: init-x\ncreated: 2026-07-01\n---\nBody.\n",
            encoding="utf-8",
        )
        (roadmap_dir / "unattached-roadmap").mkdir(parents=True)
        (roadmap_dir / "unattached-roadmap" / "OVERVIEW.md").write_text(
            "---\nstatus: active\ncreated: 2026-07-02\n---\nBody.\n",
            encoding="utf-8",
        )

        handoffs_dir = worktree / "state" / "handoffs"
        _write_handoff(
            handoffs_dir, "hoff-attached.md", roadmap_id="r1",
            extra_fields='initiative: init-x\ncreated: "2026-07-01"',
        )
        _write_handoff(
            handoffs_dir, "hoff-unattached.md", roadmap_id="r1",
            extra_fields='created: "2026-07-02"',
        )

        plans_dir = worktree / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "2026-07-01-attached-plan.md").write_text(
            "---\nstatus: implemented\ninitiative: init-x\ncreated: 2026-07-01\n---\nBody.\n",
            encoding="utf-8",
        )
        (plans_dir / "2026-07-02-unattached-plan.md").write_text(
            "---\nstatus: implemented\ncreated: 2026-07-02\n---\nBody.\n",
            encoding="utf-8",
        )

        # A non-member type — must never surface in the union even though it's
        # unattached-shaped (no initiative field at all).
        decisions_dir = worktree / "docs" / "decisions"
        decisions_dir.mkdir(parents=True)
        (decisions_dir / "not-in-union.md").write_text(
            "---\nstatus: accepted\ncreated: 2026-07-02\n---\nBody.\n", encoding="utf-8",
        )

        return git_dir, worktree

    def test_union_returns_only_unattached_across_member_types(self, unattached_repo):
        git_dir, worktree = unattached_repo
        result = _run(
            _handler(params={"unattached": True, "format": "json", "limit": 0}, repo_root=git_dir)
        )
        records = result["records"]
        titles = {r["frontmatter"].get("title") for r in records}
        types = {r["_type"] for r in records}

        assert types == {"bug", "debt", "improvement", "roadmap", "handoff", "plan"}
        assert len(records) == 6  # exactly one unattached record per member type
        for rec in records:
            assert rec["frontmatter"].get("initiative") is None

        assert "Attached bug" not in titles
        assert "Unattached bug" in titles
        assert "Unattached debt" in titles
        assert "Unattached improvement" in titles

        # Non-member type never leaks into the union.
        paths = {r["path"] for r in records}
        assert not any("not-in-union" in p for p in paths)

    def test_single_type_plus_unattached_scopes_to_that_type(self, unattached_repo):
        git_dir, worktree = unattached_repo
        result = _run(
            _handler(
                params={"type": "bug", "unattached": True, "format": "paths"},
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        basenames = {Path(p).name for p in paths}
        assert basenames == {"2026-07-02-unattached.yaml"}

    def test_sort_and_limit_apply_once_to_union_not_per_type(self, unattached_repo):
        """The load-bearing ordering: a per-type limit would silently drop
        records from types alphabetically later in UNATTACHED_TYPES before the
        union is ever assembled. limit=3 must keep the 3 earliest-`created`
        records across the WHOLE union, not the first record of each of the
        first 3 types.
        """
        git_dir, worktree = unattached_repo
        result = _run(
            _handler(
                params={
                    "unattached": True,
                    "format": "json",
                    "sort": "created",
                    "limit": 3,
                },
                repo_root=git_dir,
            )
        )
        records = result["records"]
        assert len(records) == 3
        # All 6 unattached fixtures share created=2026-07-02, so a stable sort
        # keeps the union's collection order (bug, debt, improvement, roadmap,
        # handoff, plan) for the first 3 — proves limit sliced the ASSEMBLED
        # union rather than truncating per type (a per-type limit=3 with only
        # ~1-2 unattached records per type would never even trigger a slice).
        types_kept = [r["_type"] for r in records]
        assert types_kept == ["bug", "debt", "improvement"]

    def test_per_type_scan_failure_is_skipped_not_fatal(self, unattached_repo, monkeypatch):
        """A directory-scan failure in one UNATTACHED_TYPES member must not
        abort the whole union — mirrors queryUnattachedAll's try/catch-and-
        continue (bin/query-records.js ~1520-1560).
        """
        import coordinator_core.ops.records_query as rq

        git_dir, worktree = unattached_repo
        real_collect = rq._collect_type_records

        def _boom(worktree_root, record_type):
            if record_type == "debt":
                raise rq._RecordsCollectError("simulated scan failure")
            return real_collect(worktree_root, record_type)

        monkeypatch.setattr(rq, "_collect_type_records", _boom)

        result = _run(
            _handler(params={"unattached": True, "format": "json", "limit": 0}, repo_root=git_dir)
        )
        types = {r["_type"] for r in result["records"]}
        assert "debt" not in types
        assert types == {"bug", "improvement", "roadmap", "handoff", "plan"}

    def test_no_repo_root_returns_empty_payload(self):
        result = _run(_handler(params={"unattached": True, "format": "json"}, repo_root=None))
        assert result == {"records": []}

    def test_markdown_list_uses_per_record_type_display(self, unattached_repo):
        """Per-record ``_type`` lookup picks the right renderer per record — a
        ``plan`` record (which HAS a dedicated ``_TYPE_DISPLAY`` entry in this
        module) must render with its ``— <status>`` suffix, proving the
        record_type=None global-fallback path was NOT used for every record
        (that would render every record via the bare-path default instead).
        """
        git_dir, worktree = unattached_repo
        result = _run(
            _handler(params={"unattached": True, "format": "markdown-list", "limit": 0}, repo_root=git_dir)
        )
        lines = result["records"].split("\n")
        assert any("unattached-plan.md) — implemented" in line for line in lines)


class TestParseRelativeDateUsesUtcClock:
    """``_parse_relative_date`` must anchor its cutoff to UTC ``now()``, not
    machine-local time — frontmatter dates are UTC-authored, so a local-time
    cutoff skews the ``--since``/``--older-than`` boundary by up to a day on
    any non-UTC machine (timezone- and wall-clock-hour-dependent). Injects a
    fixed clock via a ``datetime`` subclass rather than asserting against the
    runner's own timezone, per the portability requirement that this test not
    assume — or depend on setting — the runner's local timezone.
    """

    def test_cutoff_computed_from_injected_utc_now_not_naive_local_now(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        import coordinator_core.ops.records_query as rq
        from datetime import datetime, timezone

        class _FixedUtcDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                # Pre-fix code calls `datetime.now()` with no `tz` argument —
                # asserting tz is not None pins the UTC-aware call shape and
                # fails loudly (not silently-wrong-date) against the old code.
                assert tz is not None, (
                    "_parse_relative_date must call datetime.now(timezone.utc), "
                    "not naive datetime.now() — local-time cutoffs skew the "
                    "--since/--older-than boundary on non-UTC machines"
                )
                assert tz is timezone.utc
                return datetime(2026, 1, 15, 0, 30, tzinfo=tz)

        monkeypatch.setattr(rq, "datetime", _FixedUtcDateTime)

        assert rq._parse_relative_date("1d", "since") == "2026-01-14"
        assert rq._parse_relative_date("2w", "since") == "2026-01-01"
        assert rq._parse_relative_date("1m", "older-than") == "2025-12-16"

    def test_iso_literal_bypasses_clock_entirely(self):
        # Sanity: the ISO-literal branch never touches now() at all.
        import coordinator_core.ops.records_query as rq

        assert rq._parse_relative_date("2026-01-01", "since") == "2026-01-01"


class TestArchiveCoverageOptIn:
    """C2 (docs/plans/2026-08-11-pull-surface-four-columns-and-the-archive.md):
    ``records.query``'s ``include_archived`` param is OPT-IN, defaulting off.

    AC2 is the specific guarantee this class pins: the default-off result set
    for ``type=handoff`` is byte-identical to pre-change behaviour (asserted
    against an explicit expected set, not merely "non-empty" — a widened glob
    that accidentally swallowed archive files would still look "non-empty").
    The opt-in path is exercised separately, including a
    ``deployment_state=shipped`` archived record — the shape that is entirely
    invisible without this flag.
    """

    @pytest.fixture()
    def tmp_repo_with_archive(self, tmp_path: Path):
        """A minimal git repo with one live handoff and two archived handoffs
        (one carrying ``deployment_state: shipped``), month-bucketed under
        ``archive/handoffs/2026-07/`` — mirrors the real on-disk shape.
        """
        worktree = tmp_path / "repo"
        git_dir = _make_git_repo(worktree)

        _write_handoff(
            worktree / "state" / "handoffs",
            "hoff-live.md",
            roadmap_id="live-roadmap",
        )

        archive_dir = worktree / "archive" / "handoffs" / "2026-07"
        archive_dir.mkdir(parents=True)
        _write_handoff(
            archive_dir,
            "hoff-archived-shipped.md",
            roadmap_id="archived-roadmap",
            deployment_state="shipped",
        )
        _write_handoff(
            archive_dir,
            "hoff-archived-other.md",
            roadmap_id="archived-roadmap-2",
            deployment_state="abandoned",
        )

        return git_dir, worktree

    def test_default_off_result_set_for_handoff_unchanged(self, tmp_repo_with_archive):
        """AC2: omitting ``include_archived`` entirely returns EXACTLY the
        pre-existing default-off result — the one live handoff, none of the
        two archived ones — asserted as an explicit set, not a non-empty check.
        """
        git_dir, worktree = tmp_repo_with_archive
        result = _run(
            _handler(
                params={"type": "handoff", "format": "paths"},
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        basenames = {Path(p).name for p in paths}
        assert basenames == {"hoff-live.md"}

    def test_include_archived_false_explicit_matches_default_off(
        self, tmp_repo_with_archive,
    ):
        """Explicitly passing ``include_archived: False`` must match the
        omitted-param default byte-for-byte — the flag has exactly one
        off-state, not two independently-behaving ones.
        """
        git_dir, worktree = tmp_repo_with_archive
        result = _run(
            _handler(
                params={
                    "type": "handoff", "format": "paths", "include_archived": False,
                },
                repo_root=git_dir,
            )
        )
        paths = [p for p in result["records"].split("\n") if p]
        assert {Path(p).name for p in paths} == {"hoff-live.md"}

    def test_include_archived_true_picks_up_archived_records(
        self, tmp_repo_with_archive,
    ):
        """Opt-in path: both archived handoffs join the live one, including
        the ``deployment_state=shipped`` record — the shape AC2's opt-in
        coverage exists to surface.
        """
        git_dir, worktree = tmp_repo_with_archive
        result = _run(
            _handler(
                params={
                    "type": "handoff", "format": "json", "include_archived": True,
                },
                repo_root=git_dir,
            )
        )
        records = result["records"]
        basenames = {Path(r["path"]).name for r in records}
        assert basenames == {
            "hoff-live.md", "hoff-archived-shipped.md", "hoff-archived-other.md",
        }
        shipped = next(
            r for r in records if Path(r["path"]).name == "hoff-archived-shipped.md"
        )
        assert shipped["frontmatter"]["deployment_state"] == "shipped"

    def test_collect_type_records_default_matches_collect_files(self, tmp_repo_with_archive):
        """`_collect_type_records`'s default-off path collects the exact same
        file set as calling `_collect_files` directly — the merge step is
        strictly additive, never present when `include_archived` is unset.
        """
        _git_dir, worktree = tmp_repo_with_archive
        default_records = _collect_type_records(worktree, "handoff")
        direct_files = _collect_files(worktree, "handoff")
        assert {r["path"] for r in default_records} == {
            "state/handoffs/" + f.name for f in direct_files
        }
        assert len(default_records) == 1

    def test_archive_glob_map_covers_handoff_plan_memo(self):
        """Archive coverage is wired consistently across every type that has
        an archive location on disk (handoff/plan/memo), not special-cased to
        handoff alone — see archive/ directory census at fix time."""
        assert set(_ARCHIVE_GLOB_FOR_TYPE) == {"handoff", "plan", "cross-repo-memo"}
        assert _ARCHIVE_GLOB_FOR_TYPE["handoff"] == _TYPE_TO_GLOB["handoff-archived"]
        assert _ARCHIVE_GLOB_FOR_TYPE["cross-repo-memo"] == _TYPE_TO_GLOB["archived-memo"]
        assert _ARCHIVE_GLOB_FOR_TYPE["plan"] == "archive/specs/**/*.md"
