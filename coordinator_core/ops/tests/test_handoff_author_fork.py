"""
coordinator_core.ops.tests.test_handoff_author_fork — unit tests for
``coordinator_core.ops.handoff_author_fork`` (``handoff.author_fork`` op).

Coverage:
  (a) registry — "handoff.author_fork" registered after coordinator_core.ops import;
      classified MUTATING; positive-floor registry non-empty assertion.
  (b) repo_root_none — missing repo_root → error reply (no crash).
  (c) title_required — missing title param → error reply.
  (d) workstream_invalid_chars — workstream with invalid chars → error reply.
  (e) origin_session_auto_fill — CLAUDE_SESSION_ID env var → origin_session in result.
  (f) origin_handoff_from_claimed_by — fixture handoff with claimed_by matching
      session id → origin_handoff set to that handoff's "state/handoffs/<name>" path.
  (g) origin_handoff_null_when_no_match — no handoff has claimed_by → origin_handoff null.
  (f2) origin_handoff_id — C2 ID-companion: stamped from the same matched baton when it
      carries handoff_id; explicit null when the matched baton has none; explicit null
      when origin_handoff itself is null; pattern-conformant when stamped.
  (h) needs_disambiguation_plans — multiple plans, no origin_plan_id supplied → status
      "needs_disambiguation" with "plans" candidates.
  (i) needs_disambiguation_goals — multiple goals, no origin_goal_id supplied → status
      "needs_disambiguation" with "goals" candidates.
  (j) single_plan_auto_resolved — exactly one plan → auto-resolve (status ok, plan_id set).
  (k) explicit_plan_id_honoured — origin_plan_id in params → used verbatim (no matcher).
  (l) explicit_goal_id_list_honoured — origin_goal_id list in params → used verbatim.
  (m) artifact_written — on success, file written to state/handoffs/ under worktree.
  (n) provenance_fields_in_frontmatter — PROVENANCE_FIELD_MAP keys present in written file.
  (o) origin_goal_id_array_cardinality — origin_goal_id written as YAML inline-sequence.
  (p) predecessor_none — fork handoff has "predecessor: none" in frontmatter.
  (q) atomic_write_no_collision — UUID suffix prevents collision.
  (r) PROVENANCE_FIELD_MAP constant — all four logical fields present with correct cardinality.
  (s) double_ambiguous_surfaces_plans_only — both plan and goal ambiguous → only
      ``plans`` in candidates (sequential disambiguation contract, F5/F8).
  (u) count_is_not_load_bearing — op-level guard, both modes: many plans seeded and
      one clearly named → resolves anyway; a lone NON-matching plan → null plus a
      reported below-threshold degrade.  Unit coverage of the resolver itself lives
      in test_match_core.py; these pin the OP's use of it.
  (v) stamp_match_text_precedence — stamp mode's default match_text is the target's
      frontmatter title (not its filename stem), with a stem fallback that tolerates
      an absent/empty title without raising.
  (t) extract_frontmatter_scalar (F3/F4) — shared extractor: unquoted, double-quoted,
      single-quoted values all return the bare scalar; end-to-end quoted consumed_by
      resolves origin_handoff correctly.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so that ALL op
registrations fire (CBR #12 — import-ordering floor).

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C3
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects (CBR #12).
# MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass, classify
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_author_fork import (
    PROVENANCE_FIELD_MAP,
    _handler,
    _resolve_stamp_match_text,
    _stamp_fork_provenance,
)
from coordinator_core.ops.match_core import ResolutionReason

# ---------------------------------------------------------------------------
# Positive-floor registry assertion — ensures the import guard fired
# ---------------------------------------------------------------------------
assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)
assert "handoff.author_fork" in _REGISTRY, (
    "import guard failed: 'handoff.author_fork' not in _REGISTRY — "
    "coordinator_core.ops.handoff_author_fork @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Execute a coroutine synchronously (test helper)."""
    return asyncio.run(coro)


_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root``; return its common_dir (.git path)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.email", "author-fork-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.name", "Author Fork Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    return (root / ".git").resolve()


def _seed_plan(plans_dir: Path, filename: str, *, title: str, plan_id: str) -> None:
    """Write a minimal docs/plans/*.md fixture."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    content = f'---\ntitle: "{title}"\nplan_id: "{plan_id}"\nstatus: draft\n---\n\n# Body\n'
    (plans_dir / filename).write_text(content, encoding="utf-8")


def _seed_goal(goals_dir: Path, filename: str, *, title: str, goal_id: str) -> None:
    """Write a minimal state/goals/*.yaml fixture.

    goals_match._collect_goals reads *.yaml files and expects an ``id`` field
    (generic enumerator protocol key), not ``goal_id``.
    """
    goals_dir.mkdir(parents=True, exist_ok=True)
    # goals_match._collect_goals: reads *.yaml, parses frontmatter, expects 'id' + 'title' + 'status'.
    content = f"---\nid: {goal_id!r}\ntitle: {title!r}\nstatus: active\n---\n\n# Body\n"
    (goals_dir / filename).write_text(content, encoding="utf-8")


def _seed_spinoff_stub(handoffs_dir: Path, filename: str, *, title: str = "Test Spinoff") -> Path:
    """Write a minimal spinoff scaffold fixture matching the REAL
    ``ops/docgen/templates/spinoff.json`` shape d1 (``coordinator-doc-new
    --type=spinoff``) actually produces -- notably, NO origin_* fields at
    all (the docgen template never scaffolds them), which is exactly why
    the stamp path's common case is append, not replace."""
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f'title: "{title}"\n'
        "created: 2026-07-27\n"
        'branch: "none"\n'
        "status: open\n"
        "predecessor: none\n"
        "kind: spinoff\n"
        "deployment_state: ready_to_fire\n"
        "category: infra\n"
        'summary: "placeholder"\n'
        "pickup_ready: true\n"
        "---\n\n# Body\n"
    )
    path = handoffs_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


# Eight mutually-unrelated plan titles used to populate docs/plans/ for the
# candidate-count tests below.  None of them shares a token with the target
# titles those tests match against, so the only thing standing between the
# query and its intended plan is the resolver's scoring — never arity.
_UNRELATED_PLAN_FIXTURES = (
    ("2026-07-30-quantum.md", "Quantum Telemetry Shard Rebalancer", "pln-quantum"),
    ("2026-07-30-ledger.md", "Ledger Compaction Backpressure", "pln-ledger"),
    ("2026-07-30-navmesh.md", "Navmesh Streaming Budget Audit", "pln-nav"),
    ("2026-07-30-cipher.md", "Cipher Rotation Key Escrow", "pln-cipher"),
    ("2026-07-30-atlas.md", "Atlas Tile Prefetch Heuristics", "pln-atlas"),
    ("2026-07-30-freight.md", "Freight Manifest Reconciliation", "pln-freight"),
    ("2026-07-30-orchard.md", "Orchard Pruning Scheduler", "pln-orchard"),
    ("2026-07-30-basalt.md", "Basalt Column Erosion Model", "pln-basalt"),
)

# The one plan the many-candidate tests expect to win.
_TARGET_PLAN_TITLE = "Baton Supersession Hazard Rollup"
_TARGET_PLAN_ID = "pln-baton-supersession"


def _seed_unrelated_plans(plans_dir: Path) -> None:
    """Seed ``_UNRELATED_PLAN_FIXTURES`` via ``_seed_plan`` — the distractor
    corpus for the "candidate COUNT must not block resolution" tests."""
    for filename, title, plan_id in _UNRELATED_PLAN_FIXTURES:
        _seed_plan(plans_dir, filename, title=title, plan_id=plan_id)


def _seed_handoff(
    handoffs_dir: Path,
    filename: str,
    *,
    claimed_by: Optional[str] = None,
    handoff_id: Optional[str] = None,
) -> None:
    """Write a minimal state/handoffs/*.md fixture with optional claimed_by / handoff_id."""
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", 'title: "Test Handoff"', "status: open"]
    if claimed_by is not None:
        lines.append(f"claimed_by: {claimed_by}")
    if handoff_id is not None:
        lines.append(f"handoff_id: {handoff_id}")
    lines.extend(["---", "", "# Body"])
    (handoffs_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: registration and classification
# ---------------------------------------------------------------------------


class TestRegistryAndClassification:
    """Registry membership and MUTATING classification assertions."""

    def test_registry_is_non_empty(self):
        """Positive floor — registry is non-empty after coordinator_core.ops import."""
        assert len(_REGISTRY) > 0

    def test_op_registered(self):
        """'handoff.author_fork' is registered in the live _REGISTRY."""
        assert "handoff.author_fork" in _REGISTRY

    def test_op_classifies_mutating(self):
        """'handoff.author_fork' is classified MUTATING (creates a new handoff file)."""
        assert classify("handoff.author_fork") is OpClass.MUTATING

    def test_op_in_op_classification(self):
        """'handoff.author_fork' has an explicit entry in OP_CLASSIFICATION."""
        assert "handoff.author_fork" in OP_CLASSIFICATION


# ---------------------------------------------------------------------------
# Tests: PROVENANCE_FIELD_MAP constant shape
# ---------------------------------------------------------------------------


class TestProvenanceFieldMap:
    """PROVENANCE_FIELD_MAP constant has the expected shape."""

    def test_all_five_logical_fields_present(self):
        """All five logical fields are in PROVENANCE_FIELD_MAP (origin_handoff_id is the
        C2 ID-companion added alongside the four originally-ratified fields)."""
        for name in (
            "origin_session",
            "origin_handoff",
            "origin_handoff_id",
            "origin_plan_id",
            "origin_goal_id",
        ):
            assert name in PROVENANCE_FIELD_MAP, f"{name!r} missing from PROVENANCE_FIELD_MAP"

    def test_origin_goal_id_cardinality_array(self):
        """origin_goal_id has cardinality 'array' (multi-goal forks are real)."""
        assert PROVENANCE_FIELD_MAP["origin_goal_id"]["cardinality"] == "array"

    def test_scalar_fields_cardinality_scalar(self):
        """origin_session, origin_handoff, origin_handoff_id, origin_plan_id are scalar."""
        for name in ("origin_session", "origin_handoff", "origin_handoff_id", "origin_plan_id"):
            assert PROVENANCE_FIELD_MAP[name]["cardinality"] == "scalar", (
                f"{name!r} should be scalar, got {PROVENANCE_FIELD_MAP[name]['cardinality']!r}"
            )

    def test_key_entries_present(self):
        """Each entry has a 'key' field (the frontmatter key name)."""
        for name, meta in PROVENANCE_FIELD_MAP.items():
            assert "key" in meta, f"{name!r} entry is missing 'key'"
            assert meta["key"], f"{name!r} entry has empty 'key'"


# ---------------------------------------------------------------------------
# Tests: error replies
# ---------------------------------------------------------------------------


class TestErrorReplies:
    """Error conditions return {exit_code: 1, error: ...} without raising."""

    def test_repo_root_none_returns_error(self):
        """No repo_root resolved → error reply (no crash)."""
        result = _run(_handler({"title": "My Fork"}, repo_root=None))
        assert result.get("exit_code") == 1
        assert "repo_root" in result.get("error", "").lower()

    def test_title_required(self, tmp_path):
        """Missing title param → error reply."""
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({}, repo_root=common_dir))
        assert result.get("exit_code") == 1
        assert "title" in result.get("error", "").lower()

    def test_title_empty_string(self, tmp_path):
        """Empty title string → error reply."""
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({"title": "   "}, repo_root=common_dir))
        assert result.get("exit_code") == 1

    def test_workstream_invalid_chars(self, tmp_path, monkeypatch):
        """Workstream slug with invalid chars (e.g. '/') → error reply."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        # With only one plan, origin_plan_id is unambiguous so we won't hit disambiguation.
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(plans_dir, "2026-07-07-plan.md", title="A Plan", plan_id="pln-001")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-test")
        result = _run(
            _handler(
                {
                    "title": "Fork",
                    "workstream": "bad/chars",
                    "origin_plan_id": "pln-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("exit_code") == 1
        assert "workstream" in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# Tests: origin_session auto-fill via session_context
# ---------------------------------------------------------------------------


class TestOriginSessionAutoFill:
    """origin_session is resolved from the canonical env-var chain."""

    def test_origin_session_from_env(self, tmp_path, monkeypatch):
        """CLAUDE_SESSION_ID env var → origin_session in the result."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-my-001",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-env-abc1234")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = _run(
            _handler(
                {
                    "title": "My Fork",
                    "origin_plan_id": "pln-my-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        # Should succeed (single plan → auto-resolved, goal null explicit)
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_session"] == "sess-env-abc1234"

    def test_origin_session_null_when_no_env_no_sentinel(self, tmp_path, monkeypatch):
        """No env vars, no sentinel → origin_session is null in the result."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-my-002",
        )
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = _run(
            _handler(
                {
                    "title": "Fork No Session",
                    "origin_plan_id": "pln-my-002",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_session"] is None


# ---------------------------------------------------------------------------
# Tests: origin_handoff resolution from claimed_by
# ---------------------------------------------------------------------------


class TestOriginHandoffResolution:
    """origin_handoff resolved from handoff whose claimed_by matches session id."""

    def test_origin_handoff_from_claimed_by(self, tmp_path, monkeypatch):
        """Fixture handoff with claimed_by matching session id → origin_handoff set."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-consumed-xyz99"

        # Seed a single plan so origin_plan_id resolves without disambiguation.
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-my-003",
        )

        # Seed a handoff consumed by the session.
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-07-07_120000_aabbccdd.md",
            claimed_by=session_id,
        )

        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork With Handoff",
                    "origin_plan_id": "pln-my-003",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == "state/handoffs/2026-07-07_120000_aabbccdd.md"

    def test_origin_handoff_null_when_no_match(self, tmp_path, monkeypatch):
        """No handoff has claimed_by matching session id → origin_handoff is null."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-my-004",
        )
        # Seed a handoff consumed by a DIFFERENT session.
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-07_120000_zz.md",
            claimed_by="different-session-id",
        )

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-no-match")

        result = _run(
            _handler(
                {
                    "title": "Fork No Handoff",
                    "origin_plan_id": "pln-my-004",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] is None


# ---------------------------------------------------------------------------
# Tests: origin_handoff must satisfy the ratified path-shape contract
# (schema_validate.py Rule C2-1b / dag.py resolve_target), not just match a
# literal string -- this is the never-re-pin-the-bug-via-a-literal guard.
# ---------------------------------------------------------------------------


class TestOriginHandoffPathContract:
    """The stamped origin_handoff must satisfy the contract mechanically
    (Rule C2-1b, resolve_target reachability) rather than merely matching a
    hardcoded literal -- so the next shape drift fails loudly instead of
    being re-pinned by a string comparison."""

    def test_stamped_origin_handoff_passes_c2_1b_contract(self, tmp_path, monkeypatch):
        """Stamped origin_handoff round-trips through the ratified schema rule
        (Rule C2-1b, _cf_origin_handoff_path_prefix) rather than a literal
        string comparison -- the NEXT shape drift must fail this check."""
        from coordinator_core.frontmatter.schema_validate import _cf_origin_handoff_path_prefix

        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-contract-check-001"

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-contract-001",
        )
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-07_120000_contract1.md",
            claimed_by=session_id,
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork Contract Check",
                    "origin_plan_id": "pln-contract-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == "state/handoffs/2026-07-07_120000_contract1.md"
        error = _cf_origin_handoff_path_prefix({"origin_handoff": result["origin_handoff"]})
        assert error is None, f"origin_handoff failed Rule C2-1b: {error}"

    def test_resolve_target_cannot_resolve_extensionless_stem(self, tmp_path):
        """An extension-less stem (the old bug's shape) is UNRESOLVABLE by
        dag.resolve_target -- pins that "stems are just display names,
        they're fine" cannot be revived: resolve_target never appends an
        extension, so a bare stem misses every candidate tier."""
        from coordinator_core.dag import resolve_target

        repo_root = tmp_path / "repo"
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(handoffs_dir, "2026-07-07_120000_aabbccdd.md", claimed_by="sess-x")

        # The real file exists WITH its .md extension; a bare stem ref must
        # not resolve to it under any tier.
        stem_ref = "2026-07-07_120000_aabbccdd"
        resolved = resolve_target(stem_ref, str(handoffs_dir), str(repo_root))
        assert resolved is None, (
            f"extension-less stem unexpectedly resolved to {resolved!r} -- "
            "resolve_target must not treat a bare stem as a valid ref"
        )

        # Sanity check: the path-shaped ref DOES resolve, proving the miss
        # above is specifically about the missing extension/prefix, not a
        # broken fixture.
        path_ref = "state/handoffs/2026-07-07_120000_aabbccdd.md"
        resolved_path = resolve_target(path_ref, str(handoffs_dir), str(repo_root))
        assert resolved_path is not None

    def test_stamped_fork_passes_check_handoff_refs_end_to_end(self, tmp_path, monkeypatch):
        """End-to-end: stamp a fork via the self-resolving leg
        (_resolve_origin_handoff), then run _check_handoff_refs over the
        WRITTEN record with a built handoff_id_index -- zero errors. This is
        the check that would have caught the never-silently-disagree
        inversion (a bare-stem origin_handoff paired with a real
        origin_handoff_id would have surfaced as a hard error here)."""
        from coordinator_core.frontmatter.schema_validate import (
            _build_handoff_id_index,
            _check_handoff_refs,
            parse_frontmatter,
        )

        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-e2e-refs-001"

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-e2e-refs-001",
        )
        # Origin baton carries its OWN handoff_id, so the fork's
        # origin_handoff_id companion is populated too -- exercising the
        # never-silently-disagree path/id comparison, not just the dangling
        # (unresolved) path.
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-07_120000_e2erefs.md",
            claimed_by=session_id,
            handoff_id="hnd-e2e-origin-a1b2c3",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork E2E Refs Check",
                    "origin_plan_id": "pln-e2e-refs-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == "state/handoffs/2026-07-07_120000_e2erefs.md"
        assert result["origin_handoff_id"] == "hnd-e2e-origin-a1b2c3"

        record_abs_path = result["handoff_path"]
        record_repo_rel = os.path.relpath(record_abs_path, str(repo_root)).replace("\\", "/")
        content = Path(record_abs_path).read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(content)["frontmatter"]

        handoff_id_index = _build_handoff_id_index(str(repo_root))
        errors, warnings = _check_handoff_refs(
            frontmatter,
            str(repo_root),
            record_abs_path,
            record_repo_rel,
            None,
            handoff_id_index,
            False,
        )
        assert errors == [], f"unexpected referential-integrity errors: {errors}"
        assert warnings == [], f"unexpected dangling-reference warnings: {warnings}"


# ---------------------------------------------------------------------------
# Tests: origin_handoff_id (C2 ID-companion) resolution
# ---------------------------------------------------------------------------


class TestOriginHandoffIdResolution:
    """origin_handoff_id is the C2 ID-companion, derived from the SAME baton origin_handoff
    names — never a separate/independent resolution.  Null-safe on both absence paths.
    """

    _HANDOFF_ID_RE = re.compile(r"^hnd-[a-z0-9-]+-[0-9a-f]{6}$")

    def test_stamped_from_same_baton_handoff_id(self, tmp_path, monkeypatch):
        """Origin baton HAS handoff_id → origin_handoff_id stamped, equal to that id."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-hnd-id-001"

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-08-plan.md",
            title="Plan",
            plan_id="pln-hnd-id-001",
        )
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-07-08_120000_aabbccff.md",
            claimed_by=session_id,
            handoff_id="hnd-my-baton-1a2b3c",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork With Handoff Id",
                    "origin_plan_id": "pln-hnd-id-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == "state/handoffs/2026-07-08_120000_aabbccff.md"
        assert result["origin_handoff_id"] == "hnd-my-baton-1a2b3c"
        assert self._HANDOFF_ID_RE.match(result["origin_handoff_id"])

    def test_null_when_origin_baton_lacks_handoff_id(self, tmp_path, monkeypatch):
        """Origin baton matched but carries no handoff_id → explicit null, no error."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-hnd-id-002"

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-08-plan.md",
            title="Plan",
            plan_id="pln-hnd-id-002",
        )
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-07-08_120000_aabbccgg.md",
            claimed_by=session_id,
            # No handoff_id — pre-existing artifact, not backfilled.
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork No Handoff Id",
                    "origin_plan_id": "pln-hnd-id-002",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == "state/handoffs/2026-07-08_120000_aabbccgg.md"
        assert result["origin_handoff_id"] is None

    def test_null_when_origin_handoff_null(self, tmp_path, monkeypatch):
        """origin_handoff itself is null (no matching baton) → origin_handoff_id null too."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-08-plan.md",
            title="Plan",
            plan_id="pln-hnd-id-003",
        )
        # Seed a handoff consumed by a DIFFERENT session (no match).
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-08_120000_zzzzzz.md",
            claimed_by="different-session-id",
            handoff_id="hnd-not-mine-9f8e7d",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-hnd-id-no-match")

        result = _run(
            _handler(
                {
                    "title": "Fork No Origin Handoff",
                    "origin_plan_id": "pln-hnd-id-003",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] is None
        assert result["origin_handoff_id"] is None

    def test_origin_handoff_id_key_in_frontmatter(self, tmp_path, monkeypatch):
        """origin_handoff_id key present in written frontmatter alongside origin_handoff
        (add-not-swap — origin_handoff is retained unchanged)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-hnd-id-004"

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-08-plan.md",
            title="Plan",
            plan_id="pln-hnd-id-004",
        )
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-07-08_120000_aabbcc11.md",
            claimed_by=session_id,
            handoff_id="hnd-add-not-swap-a1b2c3",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork Add Not Swap",
                    "origin_plan_id": "pln-hnd-id-004",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "origin_handoff_id: hnd-add-not-swap-a1b2c3" in content
        assert "origin_handoff: state/handoffs/2026-07-08_120000_aabbcc11.md" in content


# ---------------------------------------------------------------------------
# Tests: disambiguation
# ---------------------------------------------------------------------------


class TestDisambiguation:
    """needs_disambiguation returned (not written) when multiple candidates exist."""

    def test_needs_disambiguation_plans(self, tmp_path, monkeypatch):
        """Multiple plans, origin_plan_id absent → needs_disambiguation with plan candidates."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(plans_dir, "2026-07-07-plan-a.md", title="Alpha Plan", plan_id="pln-alpha")
        _seed_plan(plans_dir, "2026-07-07-plan-b.md", title="Beta Plan",  plan_id="pln-beta")

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-disambig-test")

        result = _run(_handler({"title": "My Fork"}, repo_root=common_dir))

        assert result.get("status") == "needs_disambiguation", f"unexpected: {result}"
        candidates = result.get("candidates", {})
        assert "plans" in candidates, "should surface plan candidates"
        plan_ids = [c["plan_id"] for c in candidates["plans"]]
        assert "pln-alpha" in plan_ids
        assert "pln-beta" in plan_ids
        # No file written — directory must not exist (not created on disambiguation path).
        # Review: code-reviewer (F7) — previous assertion was vacuously True when the
        #   directory was absent (the expected state).  Assert absence directly.
        assert not (repo_root / "state" / "handoffs").exists()

    def test_needs_disambiguation_goals(self, tmp_path, monkeypatch):
        """Multiple goals, origin_goal_id absent → needs_disambiguation with goal candidates."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        # Single plan so origin_plan_id resolves unambiguously.
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-single.md",
            title="Single Plan",
            plan_id="pln-single",
        )
        # Multiple goals → ambiguity.
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(goals_dir, "goal-a.yaml", title="Alpha Goal", goal_id="gol-alpha")
        _seed_goal(goals_dir, "goal-b.yaml", title="Beta Goal",  goal_id="gol-beta")

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-goal-disambig")

        result = _run(
            _handler(
                {"title": "Fork Goals Ambig", "origin_plan_id": "pln-single"},
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "needs_disambiguation", f"unexpected: {result}"
        candidates = result.get("candidates", {})
        assert "goals" in candidates, "should surface goal candidates"
        goal_ids = [c["goal_id"] for c in candidates["goals"]]
        assert "gol-alpha" in goal_ids
        assert "gol-beta" in goal_ids

    def test_double_ambiguous_surfaces_plans_only(self, tmp_path, monkeypatch):
        """Both plans AND goals ambiguous → needs_disambiguation with only 'plans' in candidates.

        Validates the sequential contract: plan ambiguity is resolved first; goal
        ambiguity is NOT surfaced until the plan is pinned (F5 negative-spec).
        Review: code-reviewer (F8) — double-ambiguous test makes the sequential order
        observable and wire-contract-verifiable.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        # Seed two plans → plan ambiguity.
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(plans_dir, "2026-07-07-plan-p.md", title="Plan P", plan_id="pln-p")
        _seed_plan(plans_dir, "2026-07-07-plan-q.md", title="Plan Q", plan_id="pln-q")
        # Seed two goals → goal ambiguity (would surface if plan were pinned).
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(goals_dir, "goal-p.yaml", title="Goal P", goal_id="gol-p")
        _seed_goal(goals_dir, "goal-q.yaml", title="Goal Q", goal_id="gol-q")

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-double-ambig")

        result = _run(_handler({"title": "Fork"}, repo_root=common_dir))

        assert result.get("status") == "needs_disambiguation", f"unexpected: {result}"
        candidates = result.get("candidates", {})
        # Plan ambiguity resolved first — only 'plans' key present.
        assert "plans" in candidates, "plan candidates must be present"
        assert "goals" not in candidates, (
            "goal candidates must NOT appear until plan is pinned "
            "(sequential disambiguation order per F5 negative-spec)"
        )

    def test_single_plan_auto_resolved(self, tmp_path, monkeypatch):
        """Exactly one plan, whose title the fork's title/match_text clearly
        names → auto-resolved; status ok, origin_plan_id set.

        Score is load-bearing (match_core.resolve_candidate), not candidate
        COUNT — a lone candidate still has to clear AUTO_RESOLVE_MIN_SCORE,
        so this fork's title is chosen to score well against the plan's
        title rather than relying on "there was only one" alone.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-only.md",
            title="Only Plan",
            plan_id="pln-only-001",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-single-plan")

        result = _run(
            _handler(
                {"title": "Only Plan", "origin_goal_id": None},
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] == "pln-only-001"

    def test_author_mode_resolves_against_many_plans_not_blocked_by_count(
        self, tmp_path, monkeypatch
    ):
        """Author mode: candidate COUNT must not block auto-resolution.

        Nine plans are seeded; the fork's ``title`` (which is also the default
        ``match_text``) names exactly one of them.  That plan must be resolved
        and stamped.

        Negative-spec — this test must fail if any of the following returns:
        - an ``len(candidates) != 1`` arity branch deciding resolution before
          ranking (the historical defect: every repo past its second plan
          degraded ``origin_plan_id`` to null on every fork);
        - ``needs_disambiguation`` returned merely because the directory holds
          more than one plan.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_unrelated_plans(plans_dir)
        _seed_plan(
            plans_dir,
            "2026-07-30-baton-supersession.md",
            title=_TARGET_PLAN_TITLE,
            plan_id=_TARGET_PLAN_ID,
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-author-many-plans")

        result = _run(
            _handler(
                {"title": _TARGET_PLAN_TITLE, "origin_goal_id": None},
                repo_root=common_dir,
            )
        )

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] == _TARGET_PLAN_ID


# ---------------------------------------------------------------------------
# Tests: artifact write mechanics
# ---------------------------------------------------------------------------


class TestArtifactWrite:
    """Successful invocations write the handoff file with correct frontmatter."""

    def _invoke_fork(self, tmp_path, monkeypatch, *, extra_params=None):
        """Helper: init git repo, seed one plan, invoke _handler, return (repo_root, result)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-write-plan.md",
            title="Write Plan",
            plan_id="pln-write-001",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-write-abc")
        params = {
            "title": "Write Fork",
            "origin_plan_id": "pln-write-001",
            "origin_goal_id": ["gol-write-001"],
        }
        if extra_params:
            params.update(extra_params)
        result = _run(_handler(params, repo_root=common_dir))
        return repo_root, result

    def test_artifact_written_to_state_handoffs(self, tmp_path, monkeypatch):
        """On success, file is written to state/handoffs/ under the worktree."""
        repo_root, result = self._invoke_fork(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        handoffs_dir = repo_root / "state" / "handoffs"
        assert handoffs_dir.is_dir()
        md_files = list(handoffs_dir.glob("*.md"))
        assert len(md_files) == 1, f"expected 1 handoff file, got {md_files}"

    def test_minted_by_stamped_end_to_end_via_creation_door(self, tmp_path, monkeypatch):
        """C5 (person-identity-primitive-first-slice, re-done): the creation door
        resolves the operating human ITSELF (via `resolve_operating_person`,
        monkeypatched here at its import site in this module) and passes the
        result as `_normalize_one_text`'s caller-supplied `minted_by` param —
        never resolved from inside the normalizer, which is what the batch
        sweep also calls (see `test_handoff_normalize.py`'s
        `test_batch_sweep_never_stamps_minted_by_on_unrelated_handoffs` for the
        companion regression that pins the sweep side of this contract)."""
        import coordinator_core.ops.handoff_author_fork as haf

        monkeypatch.setattr(
            haf, "resolve_operating_person", lambda: {"github": "dbc-example-operator"}
        )
        repo_root, result = self._invoke_fork(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "minted_by: dbc-example-operator" in content

    def test_minted_by_absent_via_creation_door_when_resolver_unresolvable(
        self, tmp_path, monkeypatch
    ):
        """Review: coordinator:code-reviewer c71df2b9 (P2) -- the everyday case
        on a box with no `gh` auth configured: `resolve_operating_person()`
        returns an empty bundle (`{}`), `.get("github")` is `None`, and
        `minted_by` must be entirely absent from the written file at the
        creation-door level (not just proven at the `_normalize_one_text`
        unit level, see `test_minted_by_omitted_entirely_when_unresolvable`
        in `test_handoff_normalize.py`)."""
        import coordinator_core.ops.handoff_author_fork as haf

        monkeypatch.setattr(haf, "resolve_operating_person", lambda: {})
        repo_root, result = self._invoke_fork(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "minted_by" not in content

    def test_result_carries_handoff_path_and_id(self, tmp_path, monkeypatch):
        """Result carries handoff_path (absolute path) and handoff_id (stem)."""
        repo_root, result = self._invoke_fork(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        hpath = Path(result["handoff_path"])
        assert hpath.is_absolute()
        assert hpath.exists()
        assert result["handoff_id"] == hpath.stem

    def test_provenance_fields_in_frontmatter(self, tmp_path, monkeypatch):
        """PROVENANCE_FIELD_MAP keys are present in the written file frontmatter."""
        repo_root, result = self._invoke_fork(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        for logical_name, meta in PROVENANCE_FIELD_MAP.items():
            key = meta["key"]
            # Each placeholder key must appear as a YAML frontmatter field.
            assert f"{key}:" in content, (
                f"PROVENANCE key {key!r} (logical: {logical_name!r}) absent from frontmatter; "
                f"first 500 chars: {content[:500]!r}"
            )

    def test_origin_goal_id_array_cardinality_in_frontmatter(self, tmp_path, monkeypatch):
        """origin_goal_id (array cardinality) is written as a YAML inline-sequence."""
        repo_root, result = self._invoke_fork(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        # The line must start with the key and have a '[' bracket (inline-sequence or null).
        key = PROVENANCE_FIELD_MAP["origin_goal_id"]["key"]
        fm_line = next(
            (line for line in content.splitlines() if line.startswith(f"{key}:")),
            None,
        )
        assert fm_line is not None, f"{key!r} line absent from frontmatter"
        value_part = fm_line[len(f"{key}:"):].strip()
        # Should be a YAML inline-sequence or 'null'.
        assert value_part.startswith("[") or value_part == "null", (
            f"expected inline-sequence or null for {key!r}, got: {value_part!r}"
        )

    def test_session_ledger_block_appended_when_absent(self, tmp_path, monkeypatch):
        """AC2 (Review: code-reviewer 49e8b242 P2): a caller-supplied body with no
        '## Session Ledger' heading gets the canonical block appended exactly once.

        This test FAILS if the append block in handoff_author_fork.py is deleted —
        verified by hand: removing the append (and its guard) drops the count to 0.
        """
        repo_root, result = self._invoke_fork(
            tmp_path, monkeypatch, extra_params={"body": "## Some Other Section\n\ncontent"}
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert content.count("## Session Ledger") == 1, (
            f"expected exactly one '## Session Ledger' heading, "
            f"got {content.count('## Session Ledger')}; content: {content!r}"
        )

    def test_session_ledger_block_not_duplicated_when_present(self, tmp_path, monkeypatch):
        """AC2 (Review: code-reviewer 49e8b242 P2): a caller-supplied body that
        already carries a '## Session Ledger' heading is left with exactly one
        (no duplicate append)."""
        repo_root, result = self._invoke_fork(
            tmp_path,
            monkeypatch,
            extra_params={"body": "## Session Ledger\n\n<!-- pre-existing -->\n"},
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert content.count("## Session Ledger") == 1, (
            f"expected exactly one '## Session Ledger' heading (no duplicate), "
            f"got {content.count('## Session Ledger')}; content: {content!r}"
        )

    def test_predecessor_none_in_frontmatter(self, tmp_path, monkeypatch):
        """Fork handoff has 'predecessor: none' in frontmatter (not a continuation)."""
        repo_root, result = self._invoke_fork(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "predecessor: none" in content, (
            f"'predecessor: none' not found in handoff frontmatter; "
            f"first 600 chars: {content[:600]!r}"
        )

    def test_explicit_plan_id_honoured(self, tmp_path, monkeypatch):
        """Explicit origin_plan_id in params → used verbatim (not auto-resolved)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        # Seed TWO plans — without the explicit param this would trigger disambiguation.
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(plans_dir, "2026-07-07-plan-x.md", title="Plan X", plan_id="pln-x")
        _seed_plan(plans_dir, "2026-07-07-plan-y.md", title="Plan Y", plan_id="pln-y")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-explicit")

        result = _run(
            _handler(
                {
                    "title": "Fork Explicit Plan",
                    "origin_plan_id": "pln-x",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] == "pln-x"

    def test_explicit_goal_id_list_honoured(self, tmp_path, monkeypatch):
        """Explicit origin_goal_id list in params → used verbatim; no disambiguation."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-plan-only.md",
            title="Only Plan",
            plan_id="pln-only-002",
        )
        # Seed TWO goals — without the explicit param this would trigger disambiguation.
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(goals_dir, "goal-c.yaml", title="Goal C", goal_id="gol-c")
        _seed_goal(goals_dir, "goal-d.yaml", title="Goal D", goal_id="gol-d")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-explicit-goal")

        result = _run(
            _handler(
                {
                    "title": "Fork Explicit Goal",
                    "origin_plan_id": "pln-only-002",
                    "origin_goal_id": ["gol-c"],
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_goal_id"] == ["gol-c"]

    def test_atomic_write_unique_filenames(self, tmp_path, monkeypatch):
        """Two sequential fork invocations produce two uniquely-named files."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-plan-atomic.md",
            title="Atomic Plan",
            plan_id="pln-atomic-001",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-atomic")

        params = {"title": "Fork Atomic", "origin_plan_id": "pln-atomic-001", "origin_goal_id": None}
        r1 = _run(_handler(params, repo_root=common_dir))
        r2 = _run(_handler(params, repo_root=common_dir))

        assert r1.get("status") == "ok", f"first invoke failed: {r1}"
        assert r2.get("status") == "ok", f"second invoke failed: {r2}"
        # Both paths must exist and be different files.
        p1, p2 = Path(r1["handoff_path"]), Path(r2["handoff_path"])
        assert p1 != p2, "two fork invocations must produce different file paths"
        assert p1.exists() and p2.exists()


# ---------------------------------------------------------------------------
# Tests: _fm_util.extract_frontmatter_scalar (shared extractor, F3/F4)
# ---------------------------------------------------------------------------


class TestExtractFrontmatterScalar:
    """Unit tests for the shared frontmatter scalar extractor in _fm_util.

    These tests exercise the extract_frontmatter_scalar function through the
    _resolve_origin_handoff path to confirm YAML-quote stripping works end-to-end
    in the author_fork flow.  Review: code-reviewer (F4).
    """

    def test_unquoted_value_matches(self, tmp_path, monkeypatch):
        """Unquoted consumed_by value matches session id (baseline behaviour)."""
        from coordinator_core.ops._fm_util import extract_frontmatter_scalar

        text = "---\ntitle: Test\nconsumed_by: sess-abc123\n---\n\n# Body\n"
        assert extract_frontmatter_scalar(text, "consumed_by") == "sess-abc123"

    def test_double_quoted_value_stripped(self, tmp_path, monkeypatch):
        """Double-quoted consumed_by: \"sess-abc\" returns sess-abc without surrounding quotes.

        Review: code-reviewer (F4) — operators and YAML tools sometimes emit quoted scalars;
        without stripping, comparison against the unquoted resolved session id silently fails.
        """
        from coordinator_core.ops._fm_util import extract_frontmatter_scalar

        text = '---\ntitle: Test\nconsumed_by: "sess-abc123"\n---\n\n# Body\n'
        result = extract_frontmatter_scalar(text, "consumed_by")
        assert result == "sess-abc123", (
            f"expected 'sess-abc123' (no surrounding quotes), got {result!r}"
        )

    def test_single_quoted_value_stripped(self):
        """Single-quoted consumed_by: 'sess-abc' returns sess-abc without surrounding quotes."""
        from coordinator_core.ops._fm_util import extract_frontmatter_scalar

        text = "---\ntitle: Test\nconsumed_by: 'sess-abc123'\n---\n\n# Body\n"
        result = extract_frontmatter_scalar(text, "consumed_by")
        assert result == "sess-abc123", (
            f"expected 'sess-abc123' (no surrounding quotes), got {result!r}"
        )

    def test_quoted_consumed_by_resolves_origin_handoff(self, tmp_path, monkeypatch):
        """Handoff with YAML-quoted legacy consumed_by field resolves origin_handoff correctly.

        DR-084: deliberately keeps the legacy ``consumed_by`` field name — this exercises
        _resolve_origin_handoff's old-name tolerance fallback (claimed_by-or-consumed_by),
        not the new-vocabulary path already covered by TestOriginHandoffResolution.
        End-to-end test: _resolve_origin_handoff uses extract_frontmatter_scalar;
        if quotes were not stripped, consumed_by: "sess-xyz" would not match sess-xyz
        and origin_handoff would be null instead of the handoff stem.
        Review: code-reviewer (F4) — integration path through the op handler.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-quoted-xyz99"

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-my-plan.md",
            title="My Plan",
            plan_id="pln-quoted-001",
        )

        # Write handoff with YAML-quoted consumed_by (simulates operator/tool edit).
        handoffs_dir = repo_root / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            'title: "Test Handoff"\n'
            "status: active\n"
            f'consumed_by: "{session_id}"\n'
            "---\n\n# Body\n"
        )
        (handoffs_dir / "2026-07-07_120000_aabbccee.md").write_text(content, encoding="utf-8")

        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(
            _handler(
                {
                    "title": "Fork Quoted Handoff",
                    "origin_plan_id": "pln-quoted-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == "state/handoffs/2026-07-07_120000_aabbccee.md", (
            "quoted consumed_by should match after quote-stripping; "
            f"got origin_handoff={result.get('origin_handoff')!r}"
        )


# ---------------------------------------------------------------------------
# Tests: _resolve_origin_handoff fails loud on an unscannable handoffs_dir
# (silent-success audit, state/audits/2026-07-22 shape) — provenance-critical,
# so an unreadable state/handoffs/ must produce an explicit error reply, never
# a silently-null origin_handoff/origin_handoff_id.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
class TestUnscannableHandoffsDirFailsLoud:
    """An unreadable state/handoffs/ must not silently resolve origin_handoff
    to null — that would be indistinguishable from the genuine 'no origin
    handoff' case and would corrupt the fork's provenance with no visible
    signal."""

    def test_resolve_origin_handoff_raises_oserror(self, tmp_path):
        """Unit-level round-trip: _resolve_origin_handoff raises OSError (not a
        silent (None, None)) when handoffs_dir cannot be enumerated."""
        from coordinator_core.ops.handoff_author_fork import _resolve_origin_handoff

        handoffs_dir = tmp_path / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        (handoffs_dir / "unreadable.md").write_text(
            "---\nclaimed_by: sess-x\n---\n", encoding="utf-8"
        )

        # Baseline: a genuinely-empty (but readable) handoffs_dir returns (None, None)
        # cleanly, with no exception — contrasts with the unreadable case below.
        empty_dir = tmp_path / "state" / "handoffs-empty"
        empty_dir.mkdir(parents=True)
        assert _resolve_origin_handoff(empty_dir, "sess-x") == (None, None)

        original_mode = handoffs_dir.stat().st_mode
        os.chmod(handoffs_dir, 0o000)
        try:
            with pytest.raises(OSError):
                _resolve_origin_handoff(handoffs_dir, "sess-x")
        finally:
            os.chmod(handoffs_dir, original_mode)

    def test_handler_returns_explicit_error_not_null_provenance(self, tmp_path, monkeypatch):
        """Full-op integration: _handler surfaces an explicit error reply (never
        status='ok' with a silently-null origin_handoff) when state/handoffs/
        cannot be enumerated."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-22-plan.md",
            title="Plan",
            plan_id="pln-unscannable-001",
        )
        handoffs_dir = repo_root / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        (handoffs_dir / "existing.md").write_text(
            "---\nclaimed_by: some-other-session\n---\n", encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-unscannable")

        original_mode = handoffs_dir.stat().st_mode
        os.chmod(handoffs_dir, 0o000)
        try:
            result = _run(
                _handler(
                    {
                        "title": "Fork Unscannable",
                        "origin_plan_id": "pln-unscannable-001",
                        "origin_goal_id": None,
                    },
                    repo_root=common_dir,
                )
            )
        finally:
            os.chmod(handoffs_dir, original_mode)

        assert result.get("status") != "ok", (
            f"must NOT succeed with silently-null provenance when state/handoffs/ "
            f"cannot be enumerated; got {result}"
        )
        assert result.get("exit_code") == 1, result
        assert "handoffs" in result.get("error", "").lower(), result
        # No fork handoff file was written — only the pre-existing fixture file remains
        # (permissions are restored by this point via the finally block above).


# ---------------------------------------------------------------------------
# Tests: archived-twin creation guard (coordinator_core.handoff_creation_guard)
# ---------------------------------------------------------------------------


class TestArchivedTwinGuard:
    """handoff.author_fork refuses to create a live handoff sharing an
    already-archived record's filename — see
    coordinator_core.handoff_creation_guard for the shared invariant this
    delegates to. Spec backlink: state/audits/2026-07-26-handoff-live-archive-
    duplication-origin.md (example-doctrine-repo)."""

    def test_refuses_when_filename_collides_with_archived_record(self, tmp_path, monkeypatch):
        """out_path's filename already exists under archive/handoffs/ -> error, no write."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-plan.md",
            title="A Plan",
            plan_id="pln-guard-001",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-guard-abc")

        colliding_name = "2026-07-22_100600_deadbeef.md"
        archive_dir = repo_root / "archive" / "handoffs" / "2026-07"
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / colliding_name).write_text(
            "---\ntitle: \"Archived\"\nstatus: closed\n---\n\n# Body\n", encoding="utf-8"
        )

        import coordinator_core.ops.handoff_author_fork as haf

        monkeypatch.setattr(haf, "_fork_handoff_filename", lambda title: colliding_name)

        result = _run(
            _handler(
                {
                    "title": "Fork Colliding",
                    "origin_plan_id": "pln-guard-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("exit_code") == 1, result
        assert "archive" in result.get("error", "").lower(), result
        live_path = repo_root / "state" / "handoffs" / colliding_name
        assert not live_path.exists(), "guard must refuse BEFORE any write to the live path"

    def test_ordinary_fork_unaffected_when_no_archived_twin(self, tmp_path, monkeypatch):
        """No archived twin -> ordinary success path unaffected."""
        repo_root, result = self._invoke_fork_no_collision(tmp_path, monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        handoffs_dir = repo_root / "state" / "handoffs"
        assert len(list(handoffs_dir.glob("*.md"))) == 1

    def _invoke_fork_no_collision(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-plan.md",
            title="A Plan",
            plan_id="pln-noguard-001",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-noguard-abc")
        result = _run(
            _handler(
                {
                    "title": "Fork No Collision",
                    "origin_plan_id": "pln-noguard-001",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        return repo_root, result
        assert [p.name for p in handoffs_dir.glob("*.md")] == ["existing.md"], result


# ---------------------------------------------------------------------------
# Tests: STAMPING mode (params carries "handoff_path") -- the d3 rewrite
# (Option A, ratified 2026-07-27). See _handle_stamp's own docstring.
# ---------------------------------------------------------------------------


class TestStampMode:
    """handoff.author_fork's stamping mode -- writes the five origin_*
    provenance fields onto an ALREADY-EXISTING handoff file in place, rather
    than authoring a second one. Discriminated from the from-scratch author
    path purely by "handoff_path" key presence in params."""

    def test_stamps_all_five_fields_onto_existing_file_no_new_file_created(
        self, tmp_path, monkeypatch
    ):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(handoffs_dir, "2026-07-27-my-spinoff.md")

        result = _run(
            _handler(
                {
                    "handoff_path": str(target),
                    "origin_session": "sess-stamp-1",
                    "origin_handoff": "2026-07-01_100000_aaaaaaaa",
                    "origin_handoff_id": "hnd-origin-abc123",
                    "origin_plan_id": "pln-stamp-1",
                    "origin_goal_id": ["gol-stamp-1"],
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["handoff_path"] == str(target)
        assert result["handoff_id"] == target.stem
        assert result["origin_session"] == "sess-stamp-1"
        assert result["origin_handoff"] == "2026-07-01_100000_aaaaaaaa"
        assert result["origin_handoff_id"] == "hnd-origin-abc123"
        assert result["origin_plan_id"] == "pln-stamp-1"
        assert result["origin_goal_id"] == ["gol-stamp-1"]

        # Exactly ONE file -- the stamped target, not a second authored file.
        md_files = list(handoffs_dir.glob("*.md"))
        assert len(md_files) == 1, f"expected exactly one file, got {md_files}"
        assert md_files[0] == target

        content = target.read_text(encoding="utf-8")
        assert "origin_session: sess-stamp-1" in content
        assert "origin_handoff: 2026-07-01_100000_aaaaaaaa" in content
        assert "origin_handoff_id: hnd-origin-abc123" in content
        assert "origin_plan_id: pln-stamp-1" in content
        assert "origin_goal_id: [gol-stamp-1]" in content
        # Every pre-existing key, and the body, are untouched.
        assert 'title: "Test Spinoff"' in content
        assert "predecessor: none" in content
        assert "kind: spinoff" in content
        assert "status: open" in content
        assert "# Body" in content

    def test_stamps_via_repo_relative_handoff_path(self, tmp_path, monkeypatch):
        """The docstring's OTHER documented form -- ``handoff_path`` supplied
        repo-relative (``state/handoffs/<name>.md``), not absolute. Must
        resolve against the caller's worktree root (derived from the
        engine-supplied ``common_dir`` via ``main_worktree_root``), not a
        cwd-relative or parent-directory guess. Regression guard for the
        author-fork-seam-repair spinoff's diagnosis: this form must reach
        the SAME target file and stamp result as the absolute-path form
        above, never ``handoff_path not found on disk`` for an existing
        file."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(handoffs_dir, "2026-07-27-relative-spinoff.md")
        rel_path = "state/handoffs/2026-07-27-relative-spinoff.md"

        result = _run(
            _handler(
                {
                    "handoff_path": rel_path,
                    "origin_session": "sess-relative-1",
                    "origin_handoff": "2026-07-01_100000_cccccccc",
                    "origin_handoff_id": "hnd-origin-rel",
                    "origin_plan_id": "pln-relative-1",
                    "origin_goal_id": ["gol-relative-1"],
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["handoff_path"] == str(target)
        assert result["origin_session"] == "sess-relative-1"

        content = target.read_text(encoding="utf-8")
        assert "origin_session: sess-relative-1" in content
        assert "origin_handoff_id: hnd-origin-rel" in content

    def test_stamp_mode_self_resolves_when_caller_supplies_nothing(self, tmp_path, monkeypatch):
        """Falsy/absent origin_* params fall back to the SAME self-resolution
        the author path uses -- never silently left null when resolvable."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        # match_text defaults to the target's OWN title (_resolve_stamp_match_text) —
        # give the plan the same title so the lone candidate clears the
        # score floor rather than relying on candidate count alone.
        target = _seed_spinoff_stub(handoffs_dir, "2026-07-27-auto-spinoff.md", title="Only Plan")

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-27-only-plan.md",
            title="Only Plan",
            plan_id="pln-auto-only",
        )
        session_id = "sess-auto-fallback"
        _seed_handoff(
            handoffs_dir,
            "2026-07-01_090000_bbbbbbbb.md",
            claimed_by=session_id,
            handoff_id="hnd-auto-orig",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        result = _run(_handler({"handoff_path": str(target)}, repo_root=common_dir))
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_session"] == session_id
        assert result["origin_handoff"] == "state/handoffs/2026-07-01_090000_bbbbbbbb.md"
        assert result["origin_handoff_id"] == "hnd-auto-orig"
        assert result["origin_plan_id"] == "pln-auto-only"
        # Only two files exist: the seeded origin handoff and the stamped
        # target -- no third file authored.
        assert len(list(handoffs_dir.glob("*.md"))) == 2

    def test_stamp_mode_ambiguous_plan_degrades_to_null_other_fields_still_stamp(
        self, tmp_path, monkeypatch
    ):
        """Stamp mode never aborts on ambiguity -- d1's file already exists on
        disk by the time this op runs, so an early needs_disambiguation return
        would leave it permanently unstamped with no error surfaced. An
        ambiguous origin_plan_id degrades to null while every other resolvable
        field (origin_session, origin_handoff, origin_handoff_id,
        origin_goal_id) still stamps -- the degrade is reported, not silent."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(handoffs_dir, "2026-07-27-ambig-spinoff.md")

        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(plans_dir, "2026-07-27-plan-a.md", title="Alpha Plan", plan_id="pln-alpha")
        _seed_plan(plans_dir, "2026-07-27-plan-b.md", title="Beta Plan", plan_id="pln-beta")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-stamp-disambig")

        result = _run(
            _handler(
                {
                    "handoff_path": str(target),
                    "origin_handoff": "2026-07-01_100000_aaaaaaaa",
                    "origin_handoff_id": "hnd-origin-abc123",
                    "origin_goal_id": ["gol-pinned"],
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] is None
        # Other fields still resolved and stamped -- not collateral damage.
        assert result["origin_session"] == "sess-stamp-disambig"
        assert result["origin_handoff"] == "2026-07-01_100000_aaaaaaaa"
        assert result["origin_handoff_id"] == "hnd-origin-abc123"
        assert result["origin_goal_id"] == ["gol-pinned"]

        degraded = result.get("degraded")
        assert degraded, "ambiguous field must be reported in the ok payload"
        assert len(degraded) == 1
        assert degraded[0]["field"] == "origin_plan_id"
        plan_ids = {c["plan_id"] for c in degraded[0]["candidates"]}
        assert plan_ids == {"pln-alpha", "pln-beta"}

        content = target.read_text(encoding="utf-8")
        assert "origin_plan_id: null" in content
        assert "origin_handoff_id: hnd-origin-abc123" in content

    def test_stamp_mode_ambiguous_goal_degrades_to_null_other_fields_still_stamp(
        self, tmp_path, monkeypatch
    ):
        """Same degrade contract for origin_goal_id -- the second of the two
        ambiguity-prone fields."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        # match_text defaults to the target's own title -- give the lone plan
        # the same title so it clears the score floor (score is load-bearing,
        # not candidate count) and the goal branch is exercised in isolation.
        target = _seed_spinoff_stub(
            handoffs_dir, "2026-07-27-ambig-goal-spinoff.md", title="Single Plan"
        )

        # Single plan so origin_plan_id resolves unambiguously and the goal
        # branch is exercised in isolation.
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-27-single.md",
            title="Single Plan",
            plan_id="pln-single-goal-test",
        )
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(goals_dir, "goal-a.yaml", title="Alpha Goal", goal_id="gol-alpha")
        _seed_goal(goals_dir, "goal-b.yaml", title="Beta Goal", goal_id="gol-beta")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-stamp-goal-disambig")

        result = _run(
            _handler(
                {
                    "handoff_path": str(target),
                    "origin_handoff": "2026-07-01_100000_bbbbbbbb",
                    "origin_handoff_id": "hnd-origin-def456",
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] == "pln-single-goal-test"
        assert result["origin_goal_id"] is None

        degraded = result.get("degraded")
        assert degraded, "ambiguous goal field must be reported in the ok payload"
        assert len(degraded) == 1
        assert degraded[0]["field"] == "origin_goal_id"
        goal_ids = {c["goal_id"] for c in degraded[0]["candidates"]}
        assert goal_ids == {"gol-alpha", "gol-beta"}

        content = target.read_text(encoding="utf-8")
        assert "origin_goal_id: null" in content
        assert "origin_plan_id: pln-single-goal-test" in content

    def test_stamp_mode_resolves_against_many_plans_not_blocked_by_count(
        self, tmp_path, monkeypatch
    ):
        """Stamp mode: candidate COUNT must not block auto-resolution.

        Nine plans sit in ``docs/plans/``; the target stub's own frontmatter
        ``title`` names exactly one of them.  ``origin_plan_id`` must resolve
        to that plan.

        This is the defect the score-load-bearing resolver exists to fix:
        resolution used to be decided by ``len(candidates)`` in the scanned
        directory, so every repo past its second plan degraded
        ``origin_plan_id`` to null on every single fork, no matter how
        unambiguously the query named a plan.

        Negative-spec — this test must fail if a future refactor reintroduces
        an arity branch: neither a null ``origin_plan_id`` nor a ``degraded``
        entry for that field is acceptable here.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(
            handoffs_dir, "2026-07-30_141130_ab12cd34.md", title=_TARGET_PLAN_TITLE
        )

        plans_dir = repo_root / "docs" / "plans"
        _seed_unrelated_plans(plans_dir)
        _seed_plan(
            plans_dir,
            "2026-07-30-baton-supersession.md",
            title=_TARGET_PLAN_TITLE,
            plan_id=_TARGET_PLAN_ID,
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-stamp-many-plans")

        result = _run(_handler({"handoff_path": str(target)}, repo_root=common_dir))

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] == _TARGET_PLAN_ID
        degraded_fields = {d["field"] for d in result.get("degraded", [])}
        assert "origin_plan_id" not in degraded_fields, (
            f"origin_plan_id resolved cleanly; it must not be reported degraded: {result}"
        )
        assert "origin_plan_id: " + _TARGET_PLAN_ID in target.read_text(encoding="utf-8")

    def test_stamp_match_text_follows_frontmatter_title_over_filename_stem(
        self, tmp_path, monkeypatch
    ):
        """``_resolve_stamp_match_text``'s precedence, pinned at op level: the
        target stub's stem resembles plan A while its frontmatter ``title``
        resembles plan B, and the resolution must follow the TITLE.

        The stem of a real spinoff artifact is a timestamp+uuid slug that
        scores near zero against any prose plan title; the file's own ``title``
        is the only ranking text with signal in it.  Both candidates here
        would auto-resolve on their own, so a revert to ``contained.stem``
        does not fail loudly anywhere else — it just silently stamps the
        wrong plan.

        Negative-spec: resolving to ``pln-quantum`` (the stem's look-alike)
        means the frontmatter-title lookup was bypassed.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(
            handoffs_dir,
            "quantum-telemetry-shard-rebalancer.md",
            title="Coverage Dag Uncovered Ancestry Render",
        )

        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(
            plans_dir,
            "2026-07-30-quantum.md",
            title="Quantum Telemetry Shard Rebalancer",
            plan_id="pln-quantum",
        )
        _seed_plan(
            plans_dir,
            "2026-07-30-coverage.md",
            title="Coverage Dag Uncovered Ancestry Render",
            plan_id="pln-coverage",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-stamp-title-precedence")

        result = _run(_handler({"handoff_path": str(target)}, repo_root=common_dir))

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] == "pln-coverage", (
            "resolution must follow the target's frontmatter title, not its filename stem"
        )

    def test_stamp_mode_lone_non_matching_plan_does_not_auto_resolve(
        self, tmp_path, monkeypatch
    ):
        """A LONE plan whose title does not match the target's must NOT be
        auto-resolved — ``origin_plan_id`` stamps null and the degrade is
        reported as below-threshold.

        This is a deliberate contract change, not a regression.  A single
        candidate used to auto-resolve on count alone, which meant an
        unrelated plan — the only one in the directory — got stamped onto
        ``origin_plan_id`` as confident provenance that a sibling repo then
        reads as fact.  An honest null plus a machine-readable reason is
        strictly preferable to a confident wrong answer; the caller can
        re-invoke with the id pinned.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(
            handoffs_dir, "2026-07-30_150000_ef56ab78.md", title=_TARGET_PLAN_TITLE
        )

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-30-orchard.md",
            title="Orchard Pruning Scheduler",
            plan_id="pln-orchard",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-stamp-lone-nonmatch")

        result = _run(_handler({"handoff_path": str(target)}, repo_root=common_dir))

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] is None

        degraded = result.get("degraded")
        assert degraded, "the refusal must be reported, never silent"
        entry = next(d for d in degraded if d["field"] == "origin_plan_id")
        assert ResolutionReason.BELOW_THRESHOLD in entry["reason"], entry
        assert [c["plan_id"] for c in entry["candidates"]] == ["pln-orchard"]
        assert "origin_plan_id: null" in target.read_text(encoding="utf-8")

    def test_stamp_mode_target_without_usable_title_falls_back_to_stem(
        self, tmp_path, monkeypatch
    ):
        """A target whose ``title`` is present-but-empty must fall back to the
        filename stem and still stamp — ``_resolve_stamp_match_text`` never
        raises on a title it cannot use.

        The stem here deliberately names a seeded plan, so the fallback is
        observable in the result rather than merely non-fatal.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(
            handoffs_dir, "quantum-telemetry-shard-rebalancer.md", title=""
        )

        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-30-quantum.md",
            title="Quantum Telemetry Shard Rebalancer",
            plan_id="pln-quantum",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-stamp-empty-title")

        result = _run(_handler({"handoff_path": str(target)}, repo_root=common_dir))

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_plan_id"] == "pln-quantum"

    def test_resolve_stamp_match_text_tolerates_absent_frontmatter(self, tmp_path):
        """``_resolve_stamp_match_text`` on a file with NO frontmatter block
        returns the stem rather than raising.

        Asserted at function level, not through the op: ``_handle_stamp``'s
        mutate callback rejects a frontmatter-less target with a structured
        error before any stamp happens, so the op boundary cannot observe this
        branch.  The tolerance still has to hold — the helper runs BEFORE that
        rejection, and an exception there would escape as an unhandled crash
        instead of the op's own error shape.
        """
        bare = tmp_path / "2026-07-30_160000_99887766.md"
        bare.write_text("# Just a body, no frontmatter\n", encoding="utf-8")

        assert _resolve_stamp_match_text(bare) == bare.stem

    def test_author_mode_ambiguity_still_aborts_with_needs_disambiguation(
        self, tmp_path, monkeypatch
    ):
        """Regression guard: the from-scratch author path (no handoff_path in
        params) is untouched by the stamp-mode degrade -- nothing has been
        created yet on that path, so aborting on ambiguity there is still
        correct, and it must keep doing so."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(plans_dir, "2026-07-27-plan-a.md", title="Alpha Plan", plan_id="pln-alpha")
        _seed_plan(plans_dir, "2026-07-27-plan-b.md", title="Beta Plan", plan_id="pln-beta")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-author-still-aborts")

        result = _run(_handler({"title": "My Fork"}, repo_root=common_dir))

        assert result.get("status") == "needs_disambiguation", f"unexpected: {result}"
        assert "plans" in result.get("candidates", {})
        assert "degraded" not in result
        # No file written -- directory must not exist (not created on the
        # disambiguation-abort path).
        assert not (repo_root / "state" / "handoffs").exists()

    def test_stamp_mode_missing_handoff_path_is_error(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({"handoff_path": ""}, repo_root=common_dir))
        assert result.get("exit_code") == 1
        assert "handoff_path" in result.get("error", "").lower()

    def test_stamp_mode_rejects_path_outside_state_handoffs(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        outside = repo_root / "docs" / "plans" / "not-a-handoff.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text('---\ntitle: "x"\n---\n', encoding="utf-8")

        result = _run(_handler({"handoff_path": str(outside)}, repo_root=common_dir))
        assert result.get("exit_code") == 1

    def test_stamp_mode_missing_target_file_is_error(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        missing = repo_root / "state" / "handoffs" / "nope.md"
        result = _run(_handler({"handoff_path": str(missing)}, repo_root=common_dir))
        assert result.get("exit_code") == 1
        assert "not found" in result.get("error", "").lower()

    def test_stamp_mode_idempotent_restamp_no_duplicate_keys(self, tmp_path, monkeypatch):
        """Re-stamping the same file twice replaces the value in place rather
        than appending a second occurrence of the key."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        target = _seed_spinoff_stub(handoffs_dir, "2026-07-27-restamp.md")

        params = {
            "handoff_path": str(target),
            "origin_session": "sess-r",
            "origin_handoff": "origin-x",
            "origin_handoff_id": "hnd-r",
            "origin_plan_id": "pln-r",
            "origin_goal_id": ["gol-r"],
        }
        r1 = _run(_handler(params, repo_root=common_dir))
        r2 = _run(
            _handler(
                {**params, "origin_plan_id": "pln-r-updated"}, repo_root=common_dir
            )
        )
        assert r1.get("status") == "ok" and r2.get("status") == "ok", (r1, r2)

        content = target.read_text(encoding="utf-8")
        assert content.count("origin_session:") == 1
        assert content.count("origin_handoff:") == 1
        assert content.count("origin_handoff_id:") == 1
        assert content.count("origin_plan_id:") == 1
        assert content.count("origin_goal_id:") == 1
        assert "origin_plan_id: pln-r-updated" in content
        assert "pln-r\n" not in content

    def test_stamp_fork_provenance_array_branch_rejects_block_sequence_shape(self):
        """`_stamp_fork_provenance`'s array-cardinality replace branch
        (`origin_goal_id`) must inherit the SAME nested-block guard every
        scalar field in this function gets from `replace_fm_field` --
        Review: code-reviewer (P1). A block-sequence-shaped existing
        `origin_goal_id:` (hand-edited, or written by a foreign tool) must
        raise rather than being silently truncated to one bare
        `origin_goal_id: [...]` line with its indented items orphaned."""
        fm_text = (
            'title: "x"\n'
            "origin_goal_id:\n"
            "  - gol-existing-a\n"
            "  - gol-existing-b\n"
        )
        with pytest.raises(ValueError, match="origin_goal_id"):
            _stamp_fork_provenance(fm_text, {
                "origin_session": None,
                "origin_handoff": None,
                "origin_handoff_id": None,
                "origin_plan_id": None,
                "origin_goal_id": ["gol-new"],
            })

    def test_stamp_fork_provenance_array_branch_still_replaces_inline_shape(self):
        """Companion to the guard test above -- the op's own inline `[...]`
        shape (the only shape it ever writes itself) must still replace
        correctly through the now-guarded path, not regress."""
        fm_text = (
            'title: "x"\n'
            "origin_goal_id: [gol-old]\n"
        )
        new_fm = _stamp_fork_provenance(fm_text, {
            "origin_session": None,
            "origin_handoff": None,
            "origin_handoff_id": None,
            "origin_plan_id": None,
            "origin_goal_id": ["gol-new-a", "gol-new-b"],
        })
        assert "origin_goal_id: [gol-new-a, gol-new-b]" in new_fm
        assert "gol-old" not in new_fm

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_stamp_array_branch_on_empty_key_spares_the_following_line(self, eol):
        """The array-replace branch used to hand-fork the substitution regex in
        its pre-2026-07-28 `(?=[ \\t]|$)\\s*` shape. `\\s` matches a NEWLINE, so
        on a present-but-empty `origin_goal_id:` the captured prefix swallowed
        the line break and `.*$` matched the FOLLOWING line, which the
        substitution then overwrote -- silently destroying an unrelated field.
        Under CRLF the same fork failed the other way: the lookahead rejected
        the `\\r`, so the stamp silently no-op'd on a key `read_fm_field`
        reports as present. Routing through `replace_fm_field_raw` fixes both;
        both endings are asserted because an LF-only case passes either way."""
        fm_text = (
            f'title: "x"{eol}'
            f"origin_goal_id:{eol}"
            f"status: open{eol}"
        )
        new_fm = _stamp_fork_provenance(fm_text, {
            "origin_session": None,
            "origin_handoff": None,
            "origin_handoff_id": None,
            "origin_plan_id": None,
            "origin_goal_id": ["gol-new"],
        })
        assert new_fm.count("origin_goal_id:") == 1
        assert "origin_goal_id: [gol-new]" in new_fm
        # The innocent neighbour survives, value intact.
        assert "status: open" in new_fm
        assert new_fm.count("status: open") == 1
        # And the document's line endings stay uniform.
        assert ("\r\n" in new_fm) == (eol == "\r\n")
        assert "\n" not in new_fm.replace(eol, "")

    def test_stamp_mode_block_sequence_origin_goal_id_is_structured_error_not_exception(
        self, tmp_path
    ):
        """End-to-end companion for the same guard, through the full
        `_handle_stamp` op boundary: a target file with a pre-existing
        block-sequence `origin_goal_id:` must come back as this op's
        ordinary structured `_err(...)` shape (status != "ok", an `error`
        string), never an unhandled `ValueError` escaping the op --
        Review: code-reviewer (P1 + P1, the guard AND the exception
        translation)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        target = handoffs_dir / "2026-07-27-block-seq-goal.md"
        target.write_text(
            "---\n"
            'title: "Block Sequence Fixture"\n'
            "status: open\n"
            "origin_goal_id:\n"
            "  - gol-foreign-a\n"
            "  - gol-foreign-b\n"
            "---\n\n# Body\n",
            encoding="utf-8",
        )

        result = _run(
            _handler(
                {"handoff_path": str(target), "origin_goal_id": ["gol-new"]},
                repo_root=common_dir,
            )
        )
        assert result.get("status") != "ok"
        assert "origin_goal_id" in result.get("error", "")
        # Untouched -- the mutate aborted before any partial write landed.
        content = target.read_text(encoding="utf-8")
        assert "gol-foreign-a" in content
        assert "gol-new" not in content

    def test_stamp_mode_exotic_nested_block_scalar_field_is_structured_error(
        self, tmp_path
    ):
        """Same exception-translation contract (Review: code-reviewer P1) for
        one of the SCALAR provenance fields -- `origin_plan_id` pre-existing
        as a nested-block value hits `replace_fm_field`'s own guard, which
        must likewise come back as a structured `_err(...)`, not an
        unhandled `ValueError`."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        target = handoffs_dir / "2026-07-27-nested-plan-id.md"
        target.write_text(
            "---\n"
            'title: "Nested Plan Id Fixture"\n'
            "status: open\n"
            "origin_plan_id:\n"
            "  kind: exotic\n"
            "---\n\n# Body\n",
            encoding="utf-8",
        )

        result = _run(
            _handler(
                {"handoff_path": str(target), "origin_plan_id": "pln-new"},
                repo_root=common_dir,
            )
        )
        assert result.get("status") != "ok"
        assert "origin_plan_id" in result.get("error", "")
