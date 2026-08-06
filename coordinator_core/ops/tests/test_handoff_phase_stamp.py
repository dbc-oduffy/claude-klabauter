"""
coordinator_core.ops.tests.test_handoff_phase_stamp

Tests for the handoff.stamp_phase op (example-doctrine-repo-landed handoff_phase contract's
Claude-klabauter-side generation-stamp mechanism — see
docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md).

Import guard: coordinator_core.ops.handoff_phase_stamp MUST be imported at
module load time to fire @register_op("handoff.stamp_phase") and populate
_REGISTRY. Without this the decorator has not run and any registry-
completeness assertion passes vacuously over an empty entry.

Coverage:
  (a) stamped execution handoff passes claude-klabauter's own validator (C1/C2),
      including H-CROSS-EXEC-1 (four-field stamp) and H-CROSS-EXEC-2
      (kind gate) — proven BY CONSTRUCTION via the op's own post-mutation
      validate_frontmatter() gate (AC5), not a separate re-check.
  (b) the op's post-mutation validate gate rejects (MutateAbort, file
      unchanged) an attempt to stamp phase=execution onto a
      kind!=session-handoff target
  (c) ditto for a partial four-field write (missing one plan field)
  (d) phase=execution works with deployment_state ready_to_fire AND
      awaiting_gate (phase orthogonal to fireable/gated)
  (e) phase=continuation needs no four-field stamp
  (f) idempotent re-run — already-converged target is a no-op
  (g) partial-stamp-then-rerun converges to the full stamp (not skipped)
  (h) phase re-stamp: continuation -> execution overwrites to the
      requested phase + fields
  (i) missing plan_path / missing plan values fails loud when phase=execution
  (j) plan_path escape outside docs/plans/ fails loud
  (k) no git commit is issued by the op

Spec backlinks:
  - Plan C5: docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md § C5
  - Op under test: coordinator_core/ops/handoff_phase_stamp.py
  - Fixture idiom: coordinator_core/ops/tests/conftest.py (HandoffRepo)
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("handoff.stamp_phase") as a side-effect.
# MUST precede any test function so the registry is populated before assertions.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_phase_stamp  # noqa: F401 — fires @register_op

import coordinator_core.ops.handoff_phase_stamp as _mod

from coordinator_core.ipc import _REGISTRY
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.handoff_phase_stamp import _handler
from coordinator_core.frontmatter.primitives import split_frontmatter, read_fm_field
from coordinator_core.frontmatter.schema_validate import validate_frontmatter

# ---------------------------------------------------------------------------
# Registry completeness assertion
# ---------------------------------------------------------------------------

_OP_NAME = "handoff.stamp_phase"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_phase_stamp @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


# A post-cutoff date (>= 2026-07-17) so H-CROSS-EXEC-1's stamp-required rule
# actually fires — the module's own default seed created=2026-01-01 is exempt.
_POST_CUTOFF = "2026-07-17"

#: NOTE: kept free of YAML-structural characters (colons, commas, quotes,
#: brackets — see primitives._STRUCTURAL_RE) so serialize_yaml_scalar writes
#: each value UNQUOTED on both the plan and handoff sides. read_fm_field is a
#: regex-based raw-text reader (not a YAML parser); a quoted plan-side value
#: would round-trip through the op's insert_fm_field/serialize_yaml_scalar
#: call and be re-quoted on write, which is realistic op behaviour (verbatim
#: copy through the shared primitives) but would make byte-level equality
#: assertions in this file fragile for no test-value reason — mirrors how the
#: real pinned stamp on this repo's own plan frontmatter uses a bare date for
#: execution_authorized_at (no time-of-day colon).
_EXEC_STAMP = {
    "execution_authorized_by": "PM",
    "execution_authorized_at": "2026-07-17",
    "execution_authorized_sha": "00dd9804ba482f778c127de137403341cdf72a6f",
    "execution_authorized_note": "authorized handoff for execution",
}


def _write_plan(
    repo_root: Path,
    name: str,
    *,
    stamp: Optional[dict] = None,
    outside: bool = False,
) -> Path:
    """Write a minimal plan fixture with (optionally) the four execution_authorized_*
    fields in its frontmatter. Not committed — the op only reads the plan file,
    it never touches git for the plan side.

    stamp=None omits all four fields (missing-plan-values fail-loud case).
    stamp={} explicit-empty is not a supported path here — pass a dict with a
    subset of keys to simulate a PARTIAL plan stamp (missing-field fail-loud).
    """
    plans_dir = (repo_root / "outside_plans") if outside else (repo_root / "docs" / "plans")
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name

    lines = [
        f'title: "{name}"',
        "created: 2026-07-17",
    ]
    if stamp is not None:
        for field, value in stamp.items():
            lines.append(f'{field}: {value}')
    fm_block = "\n".join(lines)
    path.write_text(f"---\n{fm_block}\n---\n\n# Plan\n\nBody.\n", encoding="utf-8")
    return path


def _params(handoff_path: str, phase: str, plan_path: Optional[str] = None) -> dict:
    params = {"handoff_path": handoff_path, "phase": phase}
    if plan_path is not None:
        params["plan_path"] = plan_path
    return params


def _read_fm(path_str: str) -> str:
    """Return the fm_text of the file at path_str, or raise if no frontmatter."""
    content = open(path_str, encoding="utf-8").read()
    split = split_frontmatter(content)
    assert split is not None, f"no parseable frontmatter in {path_str}"
    return split.fm_text


def _head_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


# ---------------------------------------------------------------------------
# (a) stamped execution handoff passes claude-klabauter's own validator, by construction
# ---------------------------------------------------------------------------


def test_execution_stamp_passes_validator_h_cross_exec_1_and_2(handoff_repo):
    """A phase=execution stamp writes a handoff that validate_frontmatter()
    accepts cleanly — H-CROSS-EXEC-1 (all four fields present) and
    H-CROSS-EXEC-2 (kind==session-handoff) both satisfied.

    This is proven BY CONSTRUCTION: the op's own post-mutation gate would have
    raised MutateAbort had the write been invalid, so exit_code==0 already
    proves validity. The independent re-validate below is a belt-and-braces
    assertion that the schema and the op agree, not the sole proof.
    """
    handoff_repo.seed_handoff(
        "2026-07-17-exec.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-exec.md")
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan.md", stamp=_EXEC_STAMP)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "handoff_phase") == "execution"
    for field, value in _EXEC_STAMP.items():
        assert value in (read_fm_field(fm, field) or ""), (
            f"{field} must contain {value!r}; got {read_fm_field(fm, field)!r}"
        )

    errors = validate_frontmatter(
        __import__("yaml").safe_load(fm), _mod._SCHEMA_PATH,
    )
    assert errors == [], f"expected no schema errors; got {errors!r}"


# ---------------------------------------------------------------------------
# (b) post-mutation gate rejects phase=execution on a kind!=session-handoff target
# ---------------------------------------------------------------------------


def test_execution_stamp_rejects_wrong_kind_target(handoff_repo):
    """phase=execution onto a target whose kind != session-handoff is rejected
    (H-CROSS-EXEC-2 pre-write guard), file left byte-unchanged."""
    handoff_repo.seed_handoff(
        "2026-07-17-wrong-kind.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: spinoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-wrong-kind.md")
    original = open(abs_path, encoding="utf-8").read()
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-wrongkind.md", stamp=_EXEC_STAMP)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"wrong-kind target must be rejected; got {result!r}"
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original, (
        "rejected stamp attempt must leave the file byte-unchanged (MutateAbort)"
    )


def test_execution_stamp_rejects_absent_kind_target(handoff_repo):
    """phase=execution onto a target with NO kind field at all (kind is
    optional in the schema) is also rejected — absence != session-handoff."""
    handoff_repo.seed_handoff(
        "2026-07-17-no-kind.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-no-kind.md")
    original = open(abs_path, encoding="utf-8").read()
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-nokind.md", stamp=_EXEC_STAMP)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (c) rejects a partial four-field write
#
# The reachable manifestation of "partial four-field write" is a cited PLAN
# whose frontmatter carries only SOME of the four execution_authorized_*
# fields — the sole v1 value-source (D1) means the op can never construct a
# partial write from any other input shape. _read_plan_exec_fields's
# missing/empty check (pre-write, before locked_rmw/the mutate closure is
# ever entered) is what fails this loud; see also (i)
# test_execution_stamp_missing_plan_values_fails_loud, which asserts the same
# reachable path with a different missing field. Kept as a separate test here
# because AC5's phrasing pairs it with the wrong-kind-target rejection above.
# ---------------------------------------------------------------------------


def test_execution_stamp_rejects_partial_plan_stamp(handoff_repo):
    """A plan frontmatter carrying only SOME of the four execution_authorized_*
    fields fails loud (no write to the handoff) — the op never attempts a
    partial write onto the target file."""
    handoff_repo.seed_handoff(
        "2026-07-17-partial-write.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-partial-write.md")
    original = open(abs_path, encoding="utf-8").read()

    partial = dict(_EXEC_STAMP)
    del partial["execution_authorized_note"]
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-partial.md", stamp=partial)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"a partial four-field plan stamp must be rejected; got {result!r}"
    )
    assert result["applied"] is False
    assert "execution_authorized_note" in result.get("error", "")
    assert open(abs_path, encoding="utf-8").read() == original, (
        "rejected partial write must leave the file byte-unchanged"
    )


# ---------------------------------------------------------------------------
# (d) phase=execution works with BOTH deployment_state ready_to_fire AND
#     awaiting_gate — phase is orthogonal to fireable/gated.
# ---------------------------------------------------------------------------


def test_execution_stamp_works_with_ready_to_fire(handoff_repo):
    """phase=execution stamps cleanly onto a ready_to_fire target."""
    handoff_repo.seed_handoff(
        "2026-07-17-ready.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-ready.md")
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-ready.md", stamp=_EXEC_STAMP)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "handoff_phase") == "execution"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"


def test_execution_stamp_works_with_awaiting_gate(handoff_repo):
    """phase=execution stamps cleanly onto an awaiting_gate target — phase is
    orthogonal to fireable/gated (mirrors example-doctrine-repo AC5)."""
    handoff_repo.seed_handoff(
        "2026-07-17-gated.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="awaiting_gate",
        extra="kind: session-handoff\ngate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-gated.md")
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-gated.md", stamp=_EXEC_STAMP)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "handoff_phase") == "execution"
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate"
    assert read_fm_field(fm, "gate_dependency") is not None, (
        "stamping execution must NOT strip an unrelated gate_dependency"
    )


# ---------------------------------------------------------------------------
# (e) phase=continuation needs no four-field stamp
# ---------------------------------------------------------------------------


def test_continuation_stamp_needs_no_four_field_stamp(handoff_repo):
    """phase=continuation stamps handoff_phase without requiring (or accepting)
    plan_path / the four fields at all."""
    handoff_repo.seed_handoff(
        "2026-07-17-continuation.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-continuation.md")

    result = _run(_handler(
        _params(abs_path, "continuation"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "handoff_phase") == "continuation"
    for field in _mod._EXEC_FIELDS:
        assert read_fm_field(fm, field) is None, (
            f"continuation must not write {field}"
        )


# ---------------------------------------------------------------------------
# (f) idempotent re-run — already-converged target is a no-op
# ---------------------------------------------------------------------------


def test_execution_stamp_idempotent_at_full_target_state(handoff_repo):
    """Re-running phase=execution with the same plan on an already-fully-stamped
    target is a no-op (applied=False, byte-identical file)."""
    handoff_repo.seed_handoff(
        "2026-07-17-idempotent.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-idempotent.md")
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-idem.md", stamp=_EXEC_STAMP)

    first = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))
    assert first["exit_code"] == 0
    assert first["applied"] is True
    converged = open(abs_path, encoding="utf-8").read()

    second = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert second["exit_code"] == 0
    assert second["applied"] is False, "already-at-target must be a no-op (applied=False)"
    assert open(abs_path, encoding="utf-8").read() == converged, (
        "idempotent no-op must not modify the file"
    )


def test_execution_stamp_idempotent_with_colon_bearing_timestamp(handoff_repo):
    """Regression: the D1 convergence check at handoff_phase_stamp.py reads
    execution_authorized_* via read_fm_field_unquoted specifically because a
    colon-bearing value (e.g. an ISO timestamp with time-of-day) is written
    QUOTED by serialize_yaml_scalar (colon is a YAML structural character) —
    unlike _EXEC_STAMP above, which is deliberately kept colon-free so as not
    to exercise this path. A bare read_fm_field would never match the quoted
    on-disk value against the intended bare value, so the convergence check
    would never see "already converged" and would re-stamp on every call."""
    colon_stamp = dict(_EXEC_STAMP)
    colon_stamp["execution_authorized_at"] = "2026-07-17T10:00:00Z"

    handoff_repo.seed_handoff(
        "2026-07-17-idempotent-colon.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-idempotent-colon.md")
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-idem-colon.md", stamp=colon_stamp)

    first = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))
    assert first["exit_code"] == 0
    assert first["applied"] is True
    converged = open(abs_path, encoding="utf-8").read()

    second = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert second["exit_code"] == 0
    assert second["applied"] is False, (
        "colon-bearing execution_authorized_at must still converge to a no-op "
        "on re-run — a bare-reader regression would re-stamp every call"
    )
    assert open(abs_path, encoding="utf-8").read() == converged, (
        "idempotent no-op must not modify the file"
    )


def test_continuation_stamp_idempotent(handoff_repo):
    """phase=continuation re-run on an already-stamped target is also a no-op."""
    handoff_repo.seed_handoff(
        "2026-07-17-cont-idem.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-cont-idem.md")

    first = _run(_handler(
        _params(abs_path, "continuation"),
        repo_root=handoff_repo.common_dir,
    ))
    assert first["exit_code"] == 0
    assert first["applied"] is True
    converged = open(abs_path, encoding="utf-8").read()

    second = _run(_handler(
        _params(abs_path, "continuation"),
        repo_root=handoff_repo.common_dir,
    ))

    assert second["exit_code"] == 0
    assert second["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == converged


# ---------------------------------------------------------------------------
# (g) partial-stamp-then-rerun converges to the full stamp (not skipped)
# ---------------------------------------------------------------------------


def test_partial_stamp_then_rerun_converges_to_full_stamp(handoff_repo):
    """A target that already carries handoff_phase:execution but is missing
    the four fields (simulating a crashed prior run) CONVERGES to the full
    stamp on re-run — the idempotency guard must not key on 'is handoff_phase
    present?' alone (D1)."""
    handoff_repo.seed_handoff(
        "2026-07-17-partial-prior.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff\nhandoff_phase: execution",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-partial-prior.md")
    # Sanity: the seeded partial-prior state has handoff_phase but no four fields.
    pre_fm = _read_fm(abs_path)
    assert read_fm_field(pre_fm, "handoff_phase") == "execution"
    for field in _mod._EXEC_FIELDS:
        assert read_fm_field(pre_fm, field) is None

    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-partial-prior.md", stamp=_EXEC_STAMP)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True, "partial-stamp-then-rerun must NOT be skipped as a no-op"

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "handoff_phase") == "execution"
    for field, value in _EXEC_STAMP.items():
        assert value in (read_fm_field(fm, field) or ""), (
            f"{field} must converge to {value!r}; got {read_fm_field(fm, field)!r}"
        )


# ---------------------------------------------------------------------------
# (h) phase re-stamp: continuation -> execution overwrites to the requested
#     phase + fields
# ---------------------------------------------------------------------------


def test_phase_restamp_continuation_to_execution_overwrites(handoff_repo):
    """A target already stamped handoff_phase:continuation, now asked for
    phase=execution, overwrites to execution + the four fields (read-from-plan
    makes the target state fully deterministic — D1)."""
    handoff_repo.seed_handoff(
        "2026-07-17-restamp.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff\nhandoff_phase: continuation",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-restamp.md")
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-restamp.md", stamp=_EXEC_STAMP)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "handoff_phase") == "execution"
    for field, value in _EXEC_STAMP.items():
        assert value in (read_fm_field(fm, field) or ""), (
            f"{field} must be {value!r}; got {read_fm_field(fm, field)!r}"
        )


# ---------------------------------------------------------------------------
# (i) missing plan_path / missing plan values fails loud when phase=execution
# ---------------------------------------------------------------------------


def test_execution_stamp_missing_plan_path_param_fails_loud(handoff_repo):
    """phase=execution with NO plan_path param at all fails loud, no write."""
    handoff_repo.seed_handoff(
        "2026-07-17-no-plan-param.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-no-plan-param.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _params(abs_path, "execution"),  # no plan_path
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"missing plan_path must fail loud; got {result!r}"
    assert result["applied"] is False
    assert "plan_path" in result.get("error", "")
    assert open(abs_path, encoding="utf-8").read() == original


def test_execution_stamp_nonexistent_plan_path_fails_loud(handoff_repo):
    """phase=execution with a plan_path that resolves but does not exist on
    disk fails loud, no write."""
    handoff_repo.seed_handoff(
        "2026-07-17-plan-missing.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-plan-missing.md")
    original = open(abs_path, encoding="utf-8").read()
    (handoff_repo.root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    nonexistent = handoff_repo.root / "docs" / "plans" / "does-not-exist.md"

    result = _run(_handler(
        _params(abs_path, "execution", str(nonexistent)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_execution_stamp_missing_plan_values_fails_loud(handoff_repo):
    """phase=execution against a plan whose frontmatter is MISSING one of the
    four execution_authorized_* fields fails loud, no write to the handoff."""
    handoff_repo.seed_handoff(
        "2026-07-17-missing-value.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-missing-value.md")
    original = open(abs_path, encoding="utf-8").read()

    partial = dict(_EXEC_STAMP)
    del partial["execution_authorized_sha"]
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-missing-val.md", stamp=partial)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"missing plan field must fail loud; got {result!r}"
    assert result["applied"] is False
    assert "execution_authorized_sha" in result.get("error", "")
    assert open(abs_path, encoding="utf-8").read() == original


def test_execution_stamp_empty_plan_value_fails_loud(handoff_repo):
    """phase=execution against a plan where one field is present but EMPTY
    (whitespace-only) also fails loud."""
    handoff_repo.seed_handoff(
        "2026-07-17-empty-value.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-empty-value.md")
    original = open(abs_path, encoding="utf-8").read()

    partial = dict(_EXEC_STAMP)
    partial["execution_authorized_note"] = "   "
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-empty-val.md", stamp=partial)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"empty plan field must fail loud; got {result!r}"
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_execution_stamp_no_plan_frontmatter_fails_loud(handoff_repo):
    """A plan_path pointing at a file with NO YAML frontmatter block at all
    fails loud, no write to the handoff."""
    handoff_repo.seed_handoff(
        "2026-07-17-plan-no-fm.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-plan-no-fm.md")
    original = open(abs_path, encoding="utf-8").read()

    (handoff_repo.root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    plan_path = handoff_repo.root / "docs" / "plans" / "no-frontmatter.md"
    plan_path.write_text("# Just a plan\n\nNo frontmatter here.\n", encoding="utf-8")

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (j) plan_path escape outside docs/plans/ fails loud
# ---------------------------------------------------------------------------


def test_execution_stamp_rejects_plan_path_traversal(handoff_repo):
    """A plan_path with '../' traversal segments escaping docs/plans/ is
    rejected with exit_code=1; the out-of-tree target file is NOT read/used
    and the handoff is not mutated."""
    handoff_repo.seed_handoff(
        "2026-07-17-plan-traversal.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-plan-traversal.md")
    original = open(abs_path, encoding="utf-8").read()
    _write_plan(handoff_repo.root, "secret-plan.md", stamp=_EXEC_STAMP, outside=True)

    result = _run(_handler(
        _params(abs_path, "execution", "docs/plans/../../outside_plans/secret-plan.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"plan_path traversal must be rejected; got {result!r}"
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_execution_stamp_rejects_plan_path_out_of_tree_absolute(tmp_path, handoff_repo):
    """An out-of-tree absolute plan_path (outside docs/plans/) is rejected."""
    handoff_repo.seed_handoff(
        "2026-07-17-plan-outside-abs.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-plan-outside-abs.md")
    original = open(abs_path, encoding="utf-8").read()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_plan = outside_dir / "plan.md"
    outside_plan.write_text(
        '---\ntitle: "Outside"\nexecution_authorized_by: "PM"\n'
        'execution_authorized_at: "2026-07-17T10:00:00Z"\n'
        'execution_authorized_sha: "00dd9804ba482f778c127de137403341cdf72a6f"\n'
        'execution_authorized_note: "n"\n---\n\nBody.\n',
        encoding="utf-8",
    )

    result = _run(_handler(
        _params(abs_path, "execution", str(outside_plan)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (k) no git commit is issued by the op
# ---------------------------------------------------------------------------


def test_execution_stamp_issues_no_git_commit(handoff_repo):
    """A successful stamp does not create any new git commit — the op is a
    pure frontmatter mutation (DR-212 § 3 D2(iv))."""
    handoff_repo.seed_handoff(
        "2026-07-17-no-commit.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-no-commit.md")
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-no-commit.md", stamp=_EXEC_STAMP)

    before = _head_sha(handoff_repo.root)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True

    after = _head_sha(handoff_repo.root)
    assert after == before, "handoff.stamp_phase must not create a git commit"

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(handoff_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert "2026-07-17-no-commit.md" in status, (
        "the mutated file must appear as an UNCOMMITTED change in git status "
        "(proves the write happened but no commit was issued)"
    )


# ---------------------------------------------------------------------------
# LockTimeout -> exit_code=1 error result (fail-closed)
# ---------------------------------------------------------------------------


def _raise_lock_timeout(*args, **kwargs):
    """Replacement for locked_rmw that always raises LockTimeout (test helper)."""
    raise LockTimeout("simulated lock timeout for test")


def test_execution_stamp_lock_timeout_returns_error(handoff_repo, monkeypatch):
    """handoff.stamp_phase maps LockTimeout to exit_code=1 error result; no
    write is attempted."""
    handoff_repo.seed_handoff(
        "2026-07-17-lock-timeout.md", "claimed",
        created=_POST_CUTOFF,
        category="infra",
        summary="test fixture",
        deployment_state="ready_to_fire",
        extra="kind: session-handoff",
    )
    abs_path = handoff_repo.abs_path("2026-07-17-lock-timeout.md")
    original = open(abs_path, encoding="utf-8").read()
    plan_path = _write_plan(handoff_repo.root, "2026-07-17-plan-lock-timeout.md", stamp=_EXEC_STAMP)

    monkeypatch.setattr(_mod, "locked_rmw", _raise_lock_timeout)

    result = _run(_handler(
        _params(abs_path, "execution", str(plan_path)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"LockTimeout must map to exit_code=1 (fail-closed); got {result!r}"
    )
    assert result["applied"] is False
    error_msg = result.get("error", "")
    assert "timeout" in error_msg.lower() or "lock" in error_msg.lower(), (
        f"error message must mention 'timeout' or 'lock'; got {error_msg!r}"
    )
    assert open(abs_path, encoding="utf-8").read() == original
