"""
coordinator_core.ops.gate_validate_invocable — JSON-RPC "gate.validate_invocable"
operation: the merge-gate DoD checker (C1 skeleton of
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md).

Purpose: given a diff / changed-file set, run each of five DoD dimensions
(types, docstring presence, tests, review-stamp, latency) and return one
**tri-state** verdict per dimension — never a boolean. A crashed dimension
check reads as ERROR, never as a silent PASS (fail-closed on internal
exception; see "Fail-closed on exception" below).

**"Advisory" — the operative definition (per C1, DR-233).** This op computes
and records a verdict. It does not refuse a commit, does not block a merge,
and does not simulate a non-bypassable authority layer — DR-233 ruled
engine-native, advisory-only, no GitHub Actions/pre-commit-framework
authority layer. The verdict is a fact an operator or the strang-01 port
template (C8) can read and act on; this module never acts on it itself.

Dimension seam, not five inline implementations (C1 requirement, so C5 and
C7 can each plug in a real dimension without editing this module's dispatch
core). `_DIMENSION_REGISTRY` maps a fixed dimension name to a
`DimensionCheck` callable; `register_dimension()` lets a later chunk replace
a slot. The five slots shipped by THIS chunk all return `UNAVAILABLE`
(tests: `SKIPPED`, gated) because their tooling/wiring does not exist yet:

    types       — C2 (mypy strict-override ledger); tool not provisioned (C1b).
    docstrings  — C3 (Ruff D1xx + interrogate); tool not provisioned (C1b).
    tests       — C4 (pytest + diff-cover); additionally SKIPPED(gated) per
                  the plan's Anti-scope: "do NOT close the tests dimension
                  against an unknown-green suite before G4 lands."
    review      — C5 (review-stamp assertion against review_brightline_gate.py
                  / review_coverage_core.py); not wired yet.
    latency     — C7 (qsub-01's benchmarks/gate.py + budget-manifest.json,
                  self-exclusion sentinel); not wired yet.

Fail-closed on exception (docs/wiki/coverage-gate-perf.md § "Prior incident:
coverage.gate op missing _handler (fail-open, no-op)" — "a correctness gate
that fails open on an internal exception is worse than no gate, because it
looks green"): `_run_dimension()` wraps every dimension call in try/except
and converts ANY raised exception into a `DimensionResult` with
`verdict=Verdict.ERROR`, never letting an internal crash read as PASS
(or as a silently-dropped dimension).

Result schema: versioned via `RESULT_SCHEMA_VERSION`, see `to_json()`.
Overall verdict: ERROR if any dimension is ERROR; else FAIL if any
dimension is FAIL; else PASS if at least one dimension actually PASSed;
else UNAVAILABLE if at least one dimension is UNAVAILABLE/SKIPPED (and none
PASSed) — a run that measured nothing must not report PASS. UNAVAILABLE/
SKIPPED dimensions never flip an *already-earned* PASS away — the whole
point of an advisory, ratcheting rollout is that an unwired dimension is
silent-neutral, not gate-red, once something else has actually measured and
passed (C1b/C2/C3/C4/C5/C7 wire the real assertions; this chunk ships the
seam and stubs only).

Self-registration: importing this module calls
register_op("gate.validate_invocable", ...) as a side effect. Registered
across all five `check_registration_quad()` surfaces (see
coordinator_core/authz/classification.py, coordinator_core/op_scopes.py,
coordinator_core/ops/_registry_map.py, coordinator_core/ops/__init__.py).

COMPUTE_ONLY classification (DR-208 five-question affirmation — see the
`OP_CLASSIFICATION` entry's own comment in classification.py for the
canonical copy): the handler reads params only, invokes in-process stub
dimension callables that do no I/O, and returns a plain dict. No file is
opened for write, no git write command runs, no queue/backlog is touched,
no subprocess is spawned by any of the five stub dimensions (each is a
literal `UNAVAILABLE`/`SKIPPED` return), and there is no write-vs-read
branch.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1
"""

from __future__ import annotations

import dataclasses
import enum
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.ipc import register_op

# ---------------------------------------------------------------------------
# Op key — pinned literal string. C7 (budget-manifest entry), C8 (template
# reference), and C9 (reporting) all cite this exact string; never re-derive
# or rename it locally.
# ---------------------------------------------------------------------------
OP_KEY = "gate.validate_invocable"

# JSON result-schema version. Bump ONLY on a shape-breaking change to
# to_json()'s output (new/renamed/removed top-level or per-dimension key);
# additive optional keys do not require a bump.
RESULT_SCHEMA_VERSION = 1

# Fixed dimension order — the five DoD dimensions named by the plan. Order is
# part of the seam's contract: `dimensions` in the JSON reply is always this
# order, so a caller diffing two runs never sees dimension reordering noise.
DIMENSION_NAMES: tuple[str, ...] = ("types", "docstrings", "tests", "review", "latency")


class Verdict(str, enum.Enum):
    """Tri-state (five-valued) per-dimension verdict. Never collapse to bool.

    PASS         — the dimension's assertion ran and was satisfied.
    FAIL         — the dimension's assertion ran and was NOT satisfied.
    ERROR        — the dimension check raised (fail-closed; see module
                   docstring "Fail-closed on exception"). Never conflated
                   with FAIL: ERROR means the check itself broke, not that
                   it found a real DoD gap.
    UNAVAILABLE  — the dimension's tooling is not provisioned/wired yet
                   (this chunk's stub state for types/docstrings/review/
                   latency; also the real runtime state for any dimension
                   whose tool `shutil.which` cannot resolve).
    SKIPPED      — the dimension was deliberately not run this call, with a
                   caller-legible reason recorded in `gated_reason` (e.g.
                   the tests dimension is gated on G4 per the plan's
                   Anti-scope until a known-green baseline lands).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    SKIPPED = "SKIPPED"


@dataclasses.dataclass(frozen=True)
class DimensionResult:
    """One dimension's verdict plus a human-legible detail string.

    `gated_reason` is populated only for `Verdict.SKIPPED` — it is what
    distinguishes "SKIPPED(gated)" from a bare SKIPPED in the JSON payload
    (C1's tri-state contract literally spells it `SKIPPED(gated)`; this
    dataclass carries the parenthetical as a first-class field rather than
    folding it into `detail` prose a caller would have to string-match).
    """

    dimension: str
    verdict: Verdict
    detail: str
    gated_reason: Optional[str] = None

    def to_json(self) -> dict:
        payload = {
            "dimension": self.dimension,
            "verdict": self.verdict.value,
            "detail": self.detail,
        }
        if self.gated_reason is not None:
            payload["gated_reason"] = self.gated_reason
        return payload


# A dimension check receives the changed-file set and the diff base (both as
# passed to the op) plus repo_root, and returns a DimensionResult. It MAY
# raise — `_run_dimension()` is what makes that fail-closed, so an individual
# check implementation is free to raise on any internal error rather than
# hand-catching everything itself.
DimensionCheck = Callable[[list[str], Optional[str], Optional[Path]], DimensionResult]


def _stub_unavailable(dimension: str, reason: str) -> DimensionCheck:
    """Build a DimensionCheck that always returns UNAVAILABLE with `reason`.

    Factory, not five hand-written near-duplicate closures — each of the
    four non-tests stub dimensions below is one call to this.
    """

    def _check(changed_files: list[str], diff_base: Optional[str], repo_root: Optional[Path]) -> DimensionResult:
        return DimensionResult(dimension=dimension, verdict=Verdict.UNAVAILABLE, detail=reason)

    _check.__name__ = f"_stub_unavailable_{dimension}"
    return _check


def _tests_stub_skipped_gated(
    changed_files: list[str], diff_base: Optional[str], repo_root: Optional[Path]
) -> DimensionResult:
    """Tests dimension stub: SKIPPED(gated), not UNAVAILABLE.

    Distinct from the other four stubs on purpose — the plan's Anti-scope is
    explicit: "Do NOT close the tests dimension (C4, and C9's test leg)
    against an unknown-green suite before G4 ... lands." UNAVAILABLE would
    read as "tool not wired"; SKIPPED(gated) records the real reason (a
    deliberate policy gate, not a missing tool) so a caller reading the JSON
    payload cannot conflate the two.
    """
    return DimensionResult(
        dimension="tests",
        verdict=Verdict.SKIPPED,
        detail="tests dimension not run",
        gated_reason=(
            "gated on G4 (pinned known-green test baseline, register § 3a "
            "item 1) landing, per plan Anti-scope — C4/C9's test leg must "
            "not close against an unknown-green suite"
        ),
    )


# ---------------------------------------------------------------------------
# Dimension registry — the seam. C2 (types), C3 (docstrings), C5 (review),
# and C7 (latency) each replace their slot via register_dimension(); C4
# replaces "tests" (and lifts the SKIPPED(gated) stub once G4 lands). Do NOT
# inline a real dimension's logic here — that recreates the "five inline
# implementations" C1 explicitly rules out.
# ---------------------------------------------------------------------------
# UNREACHABLE-BY-DEFAULT, for four of the five slots. C2/C3/C5/C7 have all
# landed and each self-registers at the bottom of this module via an
# unconditional import, so "types", "docstrings", "review" and "latency" are
# replaced with their real checks before any caller can observe these stubs.
# They survive as the fallback the seam is built around -- an import that
# cannot resolve leaves UNAVAILABLE rather than a missing key -- not as a
# statement about what is wired.
#
# THEIR TEXT MUST NOT SAY "not landed". It did, naming the chunk that had
# already landed, and cost a session: reading this literal and stopping here
# yields "the review dimension is a stub, nothing enforces review coverage",
# which is false and was reported as fact to a PM and a group EM before the
# registry was inspected at runtime. `register_dimension` at the foot of this
# module is the second half of the sentence this dict starts.
_DIMENSION_REGISTRY: dict[str, DimensionCheck] = {
    "types": _stub_unavailable(
        "types", "mypy strict-override ledger unavailable (C2 landed; this stub is the "
        "fallback for a failed self-registration import)"
    ),
    "docstrings": _stub_unavailable(
        "docstrings", "Ruff D1xx + interrogate unavailable (C3 landed; this stub is the "
        "fallback for a failed self-registration import)"
    ),
    "tests": _tests_stub_skipped_gated,
    "review": _stub_unavailable(
        "review", "review-stamp assertion unavailable (C5 landed at b40126c036; this stub "
        "is the fallback for a failed self-registration import)"
    ),
    "latency": _stub_unavailable(
        "latency", "qsub-01 benchmarks/gate.py budget-manifest unavailable (C7 landed at "
        "b40126c036; this stub is the fallback for a failed self-registration import)"
    ),
}


def register_dimension(name: str, check: DimensionCheck) -> None:
    """Plug in a real dimension check for `name`, replacing its current slot.

    `name` MUST already be one of `DIMENSION_NAMES` — this registers a
    dimension's *implementation*, it does not add a sixth dimension. A later
    chunk (C2/C3/C4/C5/C7) calls this once, at its own module's import time,
    the same self-registration shape `register_op` uses elsewhere in this
    package.
    """
    if name not in DIMENSION_NAMES:
        raise ValueError(
            f"gate.validate_invocable: cannot register unknown dimension {name!r}; "
            f"must be one of {DIMENSION_NAMES}"
        )
    _DIMENSION_REGISTRY[name] = check


def _run_dimension(
    name: str, changed_files: list[str], diff_base: Optional[str], repo_root: Optional[Path]
) -> DimensionResult:
    """Invoke the registered check for `name`, fail-closed.

    Any exception raised by the check — including one from tooling this
    chunk does not control — is converted into `Verdict.ERROR`, never
    silently dropped and never mistaken for PASS. See module docstring
    "Fail-closed on exception".
    """
    check = _DIMENSION_REGISTRY[name]
    try:
        result = check(changed_files, diff_base, repo_root)
    except Exception as exc:  # noqa: BLE001 — deliberate fail-closed catch-all
        return DimensionResult(
            dimension=name,
            verdict=Verdict.ERROR,
            detail=f"{type(exc).__name__}: {exc}",
        )
    if result.dimension != name:
        # A misbehaving registered check named the wrong dimension in its
        # own result — treat that as an ERROR too rather than silently
        # relabeling it, since a caller keys the JSON payload by dimension
        # name and a mismatch would corrupt that lookup.
        return DimensionResult(
            dimension=name,
            verdict=Verdict.ERROR,
            detail=(
                f"registered check for {name!r} returned a result labeled "
                f"{result.dimension!r} — dimension/result name mismatch"
            ),
        )
    return result


def _overall_verdict(results: list[DimensionResult]) -> Verdict:
    """ERROR beats FAIL beats PASS; UNAVAILABLE/SKIPPED never flip an
    already-earned PASS away (advisory rollout — an unwired dimension is
    neutral, not gate-red, when at least one other dimension actually ran
    and passed).

    But PASS must be *earned*, not defaulted into: if nothing raised FAIL or
    ERROR, and no dimension reported PASS, and at least one dimension is
    UNAVAILABLE/SKIPPED, the honest overall is UNAVAILABLE, not PASS. A
    vacuous PASS — "no dimension measured anything, therefore green" — is
    worse than an honest UNAVAILABLE, because it reads as a real pass to a
    caller/operator who never sees the per-dimension detail (see
    docs/wiki/coverage-gate-perf.md and
    state/lessons/2026-08-07-a-gate-that-measures-a-corpus-must-not-l-*.yaml
    on gates that measure nothing and still say pass).
    """
    # Review: coordinatorcode-reviewer-4e66fb35 P2 — an empty `results` list
    # falls through every any() check to a vacuous PASS on zero measurements,
    # the same bug class be57f525e fixed for all-UNAVAILABLE/SKIPPED. Not
    # reachable from the shipped handler (DIMENSION_NAMES is fixed at 5), but
    # this is a general-purpose helper a future caller could invoke with a
    # partial/empty list.
    if not results:
        return Verdict.UNAVAILABLE
    if any(r.verdict is Verdict.ERROR for r in results):
        return Verdict.ERROR
    if any(r.verdict is Verdict.FAIL for r in results):
        return Verdict.FAIL
    if any(r.verdict is Verdict.PASS for r in results):
        return Verdict.PASS
    if any(r.verdict in (Verdict.UNAVAILABLE, Verdict.SKIPPED) for r in results):
        return Verdict.UNAVAILABLE
    return Verdict.PASS


def to_json(overall: Verdict, results: list[DimensionResult]) -> dict:
    """Build the versioned JSON result payload.

    Shape:
        {
          "schema_version": int,
          "op": "gate.validate_invocable",
          "overall": "PASS"|"FAIL"|"ERROR"|"UNAVAILABLE",
          "dimensions": [DimensionResult.to_json(), ...],  # DIMENSION_NAMES order
        }
    """
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "op": OP_KEY,
        "overall": overall.value,
        "dimensions": [r.to_json() for r in results],
    }


@register_op(OP_KEY)
def _gate_validate_invocable(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "gate.validate_invocable" handler.

    Wire params:
        changed_files (list[str], required) — the diff / changed-file set to
                                               evaluate. May be empty (a
                                               no-op diff still returns a
                                               full five-dimension verdict).
        diff_base (str, optional) — an explicit diff-base reference (e.g. a
                                     git ref/sha), passed through unexamined
                                     to whichever dimension check consumes
                                     it. Per C4's plan text this must stay an
                                     explicit op parameter, never a
                                     hardcoded `origin/main` merge-base
                                     default baked into this handler.

    Returns: the `to_json()` payload (see its docstring for shape).

    Raises ValueError if `changed_files` is missing (not merely empty —
    absent) or is not a list. Never raises for an internal dimension-check
    failure; see `_run_dimension()`.
    """
    if "changed_files" not in params:
        raise ValueError("gate.validate_invocable requires param: changed_files")
    # Review: coordinatorcode-reviewer-4e66fb35 P2 — a bare string param would
    # silently pass isinstance-free `list(str)` and split into one
    # single-character "file" per character; police shape at the same
    # boundary that already polices presence.
    if not isinstance(params["changed_files"], list):
        raise ValueError(
            "gate.validate_invocable param changed_files must be a list[str], "
            f"got {type(params['changed_files']).__name__}"
        )
    changed_files = list(params["changed_files"])
    diff_base = params.get("diff_base")

    results = [
        _run_dimension(name, changed_files, diff_base, repo_root) for name in DIMENSION_NAMES
    ]
    overall = _overall_verdict(results)
    return to_json(overall, results)


# ---------------------------------------------------------------------------
# Landed dimension-implementation imports — bottom-of-module, not top, and not
# handler-time either. Each implementation module imports names (DimensionResult,
# Verdict, register_dimension, ...) from THIS module at ITS OWN top level, so a
# top-of-module import here would be circular. Placing the import block here,
# after every name a child module imports from this file is already bound,
# resolves the cycle: by the time Python executes this line, this module is
# fully defined, so `from coordinator_core.ops.gate_validate_invocable import
# ...` inside gate_dimension_types/_review/_latency succeeds immediately.
#
# This also means `register_dimension()` calls triggered by these imports run
# exactly once, at this module's own import time — never again, and never as
# a side effect of a handler call. A caller (a test, or any other code) that
# calls `register_dimension()` AFTER this module has already been imported
# now has its registration survive: there is no runtime re-import path left
# to clobber it. (Previously `_load_dimension_implementations()` ran on every
# handler call via the import cache being a no-op after the first import —
# but "first" was whichever call happened first in process, test or handler,
# which made survival of a test's fake registration order-dependent. See
# coordinator_core/ops/tests/test_gate_validate_invocable.py and
# test_gate_dimension_types.py — both orderings, and each file alone, must
# pass.)
#
# Do NOT move this back to the top of the module "to tidy it" — that
# reintroduces the circular import this block exists to avoid.
#
# A dimension whose chunk has not landed has no module to import here and
# keeps its stub, which is the whole point of the stub being `UNAVAILABLE`
# rather than absent.
#
# Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md
# § Chunks C1 ("a dimension seam, not five inline implementations").
# ---------------------------------------------------------------------------
from coordinator_core.ops import gate_dimension_docstrings  # noqa: E402,F401
from coordinator_core.ops import gate_dimension_latency  # noqa: E402,F401
from coordinator_core.ops import gate_dimension_review  # noqa: E402,F401
from coordinator_core.ops import gate_dimension_types  # noqa: E402,F401
