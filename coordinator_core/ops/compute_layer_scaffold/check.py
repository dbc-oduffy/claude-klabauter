"""Check mode: scores Sub-shape B decision-object producers against the
compute-layer scaffolder's conformance spec.

Spec: docs/plans/2026-08-13-compute-layer-scaffolder.md, chunk C2 — discharges
AC6, AC7, AC8, AC9. Spike evidence:
docs/research/spike-verdicts/2026-08-13-compute-layer-scaffolder-emits-conformant-producer.md.

This module REPORTS. It does not migrate `pickup_assemble` or any other
producer toward conformance — that is explicit anti-scope on the plan.

AC6 (load-bearing): every clause matches BOTH reach styles a producer can use
to reach a contract symbol — bare-symbol import (`from ...envelope import
build_envelope` then `build_envelope(...)`) and module-qualified attribute
access (`from coordinator_core.contract import apply_base` then
`apply_base.execute_directives(...)`). A scorer that matches only
`ast.ImportFrom` names reports `execute_directives: 0/5`; the true fleet
answer is 5/5, because every one of the five reaches it through the
module-qualified style. `tests/test_check.py` pins both numbers so this bug
cannot silently return.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Sub-shape B — the five producers sharing one `apply_base.execute_directives`
#: engine and one closed `dict[str, Callable]` dispatch shape. The only shape
#: this check mode scores against the conformance clauses below.
SUB_SHAPE_B: tuple[str, ...] = (
    "pickup_assemble",
    "baton_assemble",
    "backlog_grind_assemble",
    "merge_assemble",
    "consolidate_assemble",
)

#: Sub-shape A — carries an explicit negative-spec against sharing
#: `apply_base`. Reported N/A per clause (AC9), never a failing grade.
SUB_SHAPE_A: tuple[str, ...] = (
    "workstream_complete",
    "workday_complete",
    "workweek_complete",
)

#: Sub-shape C — no apply half and no dispatch table at all. Reported N/A per
#: clause (AC9), never a failing grade.
SUB_SHAPE_C: tuple[str, ...] = (
    "orient_assemble",
    "review_assemble",
    "learn_lessons_assemble",
)

#: The clause the fleet never adopted. Rendered as a FLEET FINDING (AC7),
#: structurally distinct from a per-module conformance failure — see
#: `render_report`.
FLEET_FINDING_CLAUSE = "extend_exit_codes"

CLAUSES: tuple[str, ...] = (
    "closed_cli_dispatch",
    "execute_directives",
    "build_envelope",
    "no_local_emit",
    "extend_exit_codes",
)


@dataclass(frozen=True)
class SymbolReach:
    """A contract symbol identified by its defining module and name, scored
    across both reach styles a producer may use to call it (AC6)."""

    module: str
    symbol: str


#: `contract/decision_object/envelope.py::build_envelope` — AC8 baseline
#: 4/5, `pickup_assemble` is the sole miss (hand-rolls literal dicts).
BUILD_ENVELOPE = SymbolReach("coordinator_core.contract.decision_object.envelope", "build_envelope")

#: `contract/apply_base.py::execute_directives` — AC8 baseline 5/5. Every
#: producer reaches it via `apply_base.execute_directives(...)`, the
#: module-qualified style (AC6 regression oracle).
EXECUTE_DIRECTIVES = SymbolReach("coordinator_core.contract.apply_base", "execute_directives")

#: `contract/decision_object/envelope.py::extend_exit_codes` — anchors only
#: `SUCCESS = 0` and raises on a caller-supplied `SUCCESS`; codes 1/2/3 are
#: caller convention, never inherited contract. AC8 baseline 0/5 (AC7: a
#: fleet finding, not five failures).
EXTEND_EXIT_CODES = SymbolReach("coordinator_core.contract.decision_object.envelope", "extend_exit_codes")


def _iter_source_files(package_dir: Path) -> Iterable[Path]:
    """Every `.py` file under `package_dir`, excluding `tests/` and
    `__pycache__` from the walk per the plan's scoring instructions."""
    for path in sorted(package_dir.rglob("*.py")):
        rel_parts = path.relative_to(package_dir).parts
        if "tests" in rel_parts or "__pycache__" in rel_parts:
            continue
        yield path


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _reaches_symbol(tree: ast.Module, reach: SymbolReach) -> bool:
    """True if `tree` calls `reach.symbol` via EITHER reach style:

    - bare-symbol import: `from <reach.module> import <reach.symbol>` then
      a direct call `<reach.symbol>(...)`.
    - module-qualified attribute access: `from <reach.module's parent
      package> import <reach.module's tail>` then
      `<tail>.<reach.symbol>(...)`.

    AC6: matching only the first style is the exact bug that scored
    `execute_directives` at 0/5 against a true 5/5 fleet.
    """
    parent, _, module_tail = reach.module.rpartition(".")

    bare_names: set[str] = set()
    qualified_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == reach.module:
                for alias in node.names:
                    if alias.name == reach.symbol:
                        bare_names.add(alias.asname or alias.name)
            elif parent and node.module == parent:
                for alias in node.names:
                    if alias.name == module_tail:
                        qualified_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == reach.module:
                    qualified_names.add(alias.asname or alias.name.rsplit(".", 1)[-1])

    if not bare_names and not qualified_names:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in bare_names:
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr == reach.symbol
            and isinstance(func.value, ast.Name)
            and func.value.id in qualified_names
        ):
            return True
    return False


def _has_closed_cli_dispatch(tree: ast.Module) -> bool:
    """A module-level `_CLI_DISPATCH` bound to a dict LITERAL — never a
    `getattr`/`import_module` dispatch path (AC5's negative-spec)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target] if node.target is not None else []
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_CLI_DISPATCH" and isinstance(value, ast.Dict):
                return True
    return False


def _has_local_emit(tree: ast.Module) -> bool:
    """True if this file re-implements its own `_emit` rather than composing
    `build_envelope`."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_emit":
            return True
    return False


@dataclass(frozen=True)
class ModuleScore:
    """Per-clause result for one module. `None` on every clause means the
    module is outside Sub-shape B and the clause is N/A (AC9) — never
    rendered as a failing grade."""

    module: str
    shape: str
    closed_cli_dispatch: Optional[bool]
    execute_directives: Optional[bool]
    build_envelope: Optional[bool]
    no_local_emit: Optional[bool]
    extend_exit_codes: Optional[bool]

    def clause(self, name: str) -> Optional[bool]:
        return getattr(self, name)

    def clean(self) -> Optional[bool]:
        if self.shape != "B":
            return None
        return all(self.clause(c) for c in CLAUSES)


def _shape_of(module_name: str) -> str:
    if module_name in SUB_SHAPE_B:
        return "B"
    if module_name in SUB_SHAPE_A:
        return "A"
    if module_name in SUB_SHAPE_C:
        return "C"
    raise ValueError(f"unregistered compute-layer module: {module_name!r}")


def score_module(module_name: str, repo_root: Path = REPO_ROOT) -> ModuleScore:
    """Score one module. Sub-shape A/C modules short-circuit to an explicit
    N/A axis (AC9) — a module with no apply half has nothing to walk, and a
    module with a documented negative-spec against `apply_base` is not being
    graded for the choice not to share it."""
    shape = _shape_of(module_name)
    if shape != "B":
        return ModuleScore(module_name, shape, None, None, None, None, None)

    package_dir = repo_root / "coordinator_core" / module_name
    trees = [_parse(p) for p in _iter_source_files(package_dir)]

    return ModuleScore(
        module=module_name,
        shape=shape,
        closed_cli_dispatch=any(_has_closed_cli_dispatch(t) for t in trees),
        execute_directives=any(_reaches_symbol(t, EXECUTE_DIRECTIVES) for t in trees),
        build_envelope=any(_reaches_symbol(t, BUILD_ENVELOPE) for t in trees),
        no_local_emit=not any(_has_local_emit(t) for t in trees),
        extend_exit_codes=any(_reaches_symbol(t, EXTEND_EXIT_CODES) for t in trees),
    )


@dataclass(frozen=True)
class FleetReport:
    scores: tuple[ModuleScore, ...]

    def clause_tally(self, clause: str) -> tuple[int, int]:
        """`(passed, applicable)` over only the modules for which `clause`
        is applicable — Sub-shape A/C modules never widen the denominator
        (AC9)."""
        applicable = [s for s in self.scores if s.clause(clause) is not None]
        passed = sum(1 for s in applicable if s.clause(clause))
        return passed, len(applicable)

    def clean_on_all(self) -> tuple[int, int]:
        applicable = [s for s in self.scores if s.shape == "B"]
        clean = sum(1 for s in applicable if s.clean())
        return clean, len(applicable)


def score_fleet(modules: Iterable[str] = SUB_SHAPE_B, repo_root: Path = REPO_ROOT) -> FleetReport:
    return FleetReport(scores=tuple(score_module(m, repo_root) for m in modules))


def render_report(report: FleetReport) -> str:
    """Renders per-module conformance clauses, then the `extend_exit_codes`
    ladder as a visually and structurally separate FLEET FINDING (AC7) — a
    0/5 clause nobody adopted is not five module failures."""
    lines = ["Sub-shape B conformance:"]
    for clause in ("closed_cli_dispatch", "execute_directives", "build_envelope", "no_local_emit"):
        passed, total = report.clause_tally(clause)
        lines.append(f"  {clause}: {passed}/{total}")
    clean, total = report.clean_on_all()
    lines.append(f"  clean_on_all: {clean}/{total}")
    lines.append("")
    lines.append("FLEET FINDING (not a per-module conformance failure):")
    fp, ft = report.clause_tally(FLEET_FINDING_CLAUSE)
    lines.append(
        f"  {FLEET_FINDING_CLAUSE} uptake: {fp}/{ft} — extend_exit_codes anchors only "
        "SUCCESS=0 and raises on a caller-supplied SUCCESS; codes 1/2/3 were never "
        "inherited contract"
    )
    return "\n".join(lines)
