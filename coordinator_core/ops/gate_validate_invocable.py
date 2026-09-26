"""
coordinator_core.ops.gate_validate_invocable — JSON-RPC "gate.validate_invocable"
operation: the merge-gate DoD checker (C1 skeleton of
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md).

Purpose: given a diff / changed-file set, run each of five DoD dimensions
(types, docstring presence, tests, review-stamp, latency) and return one
**tri-state** verdict per dimension — never a boolean. A crashed dimension
check reads as ERROR, never as a silent PASS (fail-closed on internal
exception; see "Fail-closed on exception" below).

**"Advisory" — the operative definition (per C1, DR-233, superseded by
DR-375).** This op computes and records a verdict. It does not refuse a
commit and does not simulate a non-bypassable authority layer at this local
check — DR-233 ruled engine-native, advisory-only, no GitHub
Actions/pre-commit-framework authority layer, and DR-375 leaves that local
posture in place while adding a separate remote-authority layer (a GitHub
ruleset plus a self-attested local status) at `git push`. The verdict is a
fact an operator or the strang-01 port template (C8) can read and act on;
this module never acts on it itself.

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

OP_KEY = "gate.validate_invocable"

RESULT_SCHEMA_VERSION = 1

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


# UNREACHABLE-BY-DEFAULT, for four of the five slots. C2/C3/C5/C7 have all
# cannot resolve leaves UNAVAILABLE rather than a missing key -- not as a
_DIMENSION_REGISTRY: dict[str, DimensionCheck] = {
    "types": _stub_unavailable(
        "types", "mypy strict-override ledger unavailable (C2 landed; this slot is "
        "replaced below by an unconditional self-registration import, which raises "
        "at module load rather than leaving this stub in place if it fails)"
    ),
    "docstrings": _stub_unavailable(
        "docstrings", "Ruff D1xx + interrogate unavailable (C3 landed; this slot is "
        "replaced below by an unconditional self-registration import, which raises "
        "at module load rather than leaving this stub in place if it fails)"
    ),
    "tests": _tests_stub_skipped_gated,
    "review": _stub_unavailable(
        "review", "review-stamp assertion unavailable (C5 landed at b40126c036; this "
        "slot is replaced below by an unconditional self-registration import, which "
        "raises at module load rather than leaving this stub in place if it fails)"
    ),
    "latency": _stub_unavailable(
        "latency", "qsub-01 benchmarks/gate.py budget-manifest unavailable (C7 landed "
        "at b40126c036; this slot is replaced below by an unconditional "
        "self-registration import, which raises at module load rather than leaving "
        "this stub in place if it fails)"
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
    # the same bug class be57f525e fixed for all-UNAVAILABLE/SKIPPED. Not
    # reachable from the shipped handler (DIMENSION_NAMES is fixed at 5), but
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


# keeps its stub, which is the whole point of the stub being `UNAVAILABLE`
from coordinator_core.ops import gate_dimension_docstrings  # noqa: E402,F401
from coordinator_core.ops import gate_dimension_latency  # noqa: E402,F401
from coordinator_core.ops import gate_dimension_review  # noqa: E402,F401
from coordinator_core.ops import gate_dimension_types  # noqa: E402,F401
