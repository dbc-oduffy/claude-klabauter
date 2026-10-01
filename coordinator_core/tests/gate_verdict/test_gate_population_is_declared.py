"""Guard: the gate-verdict POPULATION stays complete, every claim resolves, the pending ceiling is exact.

AST only: no subprocess, no import of the replay modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.tests.gate_verdict.population import (
    PENDING_REPLAY_CEILING,
    POPULATION,
)

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
_DISPATCHABLE = _REPO / "coordinator_core/authz/dispatchable.py"
_NAMED_KEYS = frozenset(
    {
        "workstream-complete-apply",
        "vendored-schema-drift",
        "percolate-round",
        "baton-assemble-apply",
    }
)
_POPULATION_FILE = "coordinator_core/tests/gate_verdict/population.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _dispatchable_verbs() -> set[str]:
    verbs: set[str] = set()
    for node in _parse(_DISPATCHABLE).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "ASSEMBLER_DISPATCHABLE":
            for sets in node.value.args[0].values:
                for sub in ast.walk(sets):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        verbs.add(sub.value)
            return verbs
    raise AssertionError("ASSEMBLER_DISPATCHABLE not found in coordinator_core/authz/dispatchable.py")


def _claims() -> dict[str, list[str]]:
    claims: dict[str, list[str]] = {}
    for path in sorted(_HERE.glob("test_replay_*.py")):
        for node in _parse(path).body:
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "GATE_VERDICT_CASE" for t in node.targets)
                and isinstance(node.value, ast.Constant)
            ):
                claims.setdefault(node.value.value, []).append(path.name)
    return claims


def test_population_is_exactly_the_dispatchable_verbs_plus_named_keys():
    expected = _dispatchable_verbs() | _NAMED_KEYS
    actual = set(POPULATION)
    missing = sorted(expected - actual)
    stale = sorted(actual - expected)
    assert not missing, (
        f"add a line to POPULATION in {_POPULATION_FILE} for each of: {missing}"
    )
    assert not stale, (
        f"delete the POPULATION line in {_POPULATION_FILE} for each of: {stale}"
    )


def test_every_replay_entry_has_exactly_one_claiming_test_and_vice_versa():
    claims = _claims()
    replay = {k for k, v in POPULATION.items() if v[0] == "replay"}
    for key in sorted(replay):
        files = claims.get(key, [])
        assert len(files) == 1, (
            f"POPULATION['{key}'] is replay but {len(files)} test_replay_*.py claim it ({files}); "
            f"add exactly one `GATE_VERDICT_CASE = \"{key}\"` or change its kind in {_POPULATION_FILE}"
        )
    for key, files in sorted(claims.items()):
        assert key in replay, (
            f"{files} claim GATE_VERDICT_CASE = \"{key}\" but POPULATION has it as "
            f"{POPULATION.get(key, ('absent',))[0]}; set it to a 'replay' entry in {_POPULATION_FILE}"
        )


def test_every_covered_by_resolves_to_a_defined_test():
    for key, (kind, detail) in sorted(POPULATION.items()):
        if kind != "covered_by":
            continue
        rel, _, name = detail.partition("::")
        path = _REPO / rel
        assert path.is_file(), (
            f"POPULATION['{key}'] covered_by file '{rel}' does not exist; fix the path in {_POPULATION_FILE}"
        )
        defined = {
            n.name
            for n in ast.walk(_parse(path))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        assert name and name in defined, (
            f"POPULATION['{key}'] covered_by '{name}' is not defined in {rel}; fix the name in {_POPULATION_FILE}"
        )


def test_pending_count_equals_ceiling_exactly():
    pending = sorted(k for k, v in POPULATION.items() if v[0] == "pending_replay")
    assert len(pending) == PENDING_REPLAY_CEILING, (
        f"{len(pending)} pending_replay entries vs PENDING_REPLAY_CEILING={PENDING_REPLAY_CEILING}; "
        f"set PENDING_REPLAY_CEILING to {len(pending)} in {_POPULATION_FILE} "
        f"(lower it when a replay lands, never raise it to admit a new pending)"
    )
