"""No value in fleet-mode.json can change how a denial-class guard behaves.

Runtime half of the fleet-mode reader floor (the static half is
`test_fleet_mode_reader_set.py`). Every denial-class guard is run on its
fixtures twice, once as shipped and once with every public function of
`coordinator_core.session.fleet_mode` and `.mode_resolution` replaced by one
that raises, and the verdicts must be identical. The fixtures must also make
each guard deny at least once and allow at least once, otherwise the patched run
would prove nothing about the guard's decision path.

Trap: a guard that reads the record by file path, not through those modules,
is invisible here; the static reader scan is what catches it.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinator_core.tests.fleet_mode_independence_fixtures import (
    ALL_FIXTURES,
    bash_guard_cases,
    is_deny,
    write_guard_cases,
)

_GUARD_DIRS = ("write_guards", "bash_guards")
_FLEET_MODULES = (
    "coordinator_core.session.fleet_mode",
    "coordinator_core.session.mode_resolution",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_level_names(tree: ast.Module) -> tuple[dict[str, ast.expr | None], bool]:
    bound: dict[str, ast.expr | None] = {}
    has_check = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound[node.target.id] = node.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "check":
            has_check = True
    return bound, has_check


_ADVISORY_ONLY = (
    "coordinator_core.bash_guards.guard_grep_via_bash",  # advisory-only, never denies
    "coordinator_core.bash_guards.guard_powershell_via_bash",  # advisory-only, never denies
    "coordinator_core.bash_guards.guard_multiprobe_banner",  # deny leg retired (DR-280)
    "coordinator_core.bash_guards.guard_plumbing_and_loops",  # deny leg retired (DR-280)
)


def denial_class_guards(root: Path) -> list[str]:
    """Dotted names of the denial-class guard modules under `root`.

    A top-level `write_guards/*.py` or `bash_guards/*.py` module that binds a
    module-level `MATCHERS`, defines `check`, and whose module-level `CLASS` is
    `"hard-deny"` or is not a derivable string literal (absent, or computed),
    minus `_ADVISORY_ONLY`.
    """
    found: list[str] = []
    for guard_dir in _GUARD_DIRS:
        for path in sorted((root / "coordinator_core" / guard_dir).glob("*.py")):
            if f"coordinator_core.{guard_dir}.{path.stem}" in _ADVISORY_ONLY:
                continue
            bound, has_check = _module_level_names(ast.parse(path.read_text(encoding="utf-8")))
            if "MATCHERS" not in bound or not has_check:
                continue
            cls = bound.get("CLASS")
            derivable = isinstance(cls, ast.Constant) and isinstance(cls.value, str)
            if derivable and cls.value != "hard-deny":
                continue
            found.append(f"coordinator_core.{guard_dir}.{path.stem}")
    return found


def _fleet_functions() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for mod_name in _FLEET_MODULES:
        mod = importlib.import_module(mod_name)
        for name, obj in vars(mod).items():
            if name.startswith("_") or not inspect.isfunction(obj) or obj.__module__ != mod_name:
                continue
            out.append((name, obj))
    return out


def _raise_on_fleet_read(name: str):
    def _raiser(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"fleet-mode read reached: {name}")

    return _raiser


def _patch_fleet_reads(mp: pytest.MonkeyPatch) -> int:
    """Make every public fleet-mode function raise, in its home module and in
    every loaded `coordinator_core` module that bound the same function by name.
    Returns the number of bindings replaced.
    """
    replaced = 0
    functions = _fleet_functions()
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("coordinator_core"):
            continue
        namespace = getattr(module, "__dict__", {})
        for name, obj in functions:
            if namespace.get(name) is obj:
                mp.setattr(module, name, _raise_on_fleet_read(name))
                replaced += 1
    return replaced


def _normalised(verdict: Any, root: Path) -> str:
    text = repr(verdict)
    for spelling in {str(root), root.as_posix(), str(root).replace("\\", "\\\\")}:
        text = text.replace(spelling, "<ROOT>")
    return text


def _run_case(guard: Any, case: Any, root: Path, *, patched: bool) -> tuple[Any, str]:
    root.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        payload = case(root, mp)
        if patched:
            _patch_fleet_reads(mp)
        try:
            verdict = guard.check(payload)
        except Exception as exc:  # a raise is a verdict the comparison must see
            return exc, f"RAISED {type(exc).__name__}: {exc}"
    return verdict, _normalised(verdict, root)


def _independence_failures(guard: Any, label: str, case: Any, tmp_path: Path) -> list[str]:
    plain, plain_text = _run_case(guard, case, tmp_path / "plain", patched=False)
    patched, patched_text = _run_case(guard, case, tmp_path / "patched", patched=True)
    if plain_text != patched_text:
        return [f"{label}: verdict changed with fleet-mode reads raising\n  plain:   {plain_text}\n  patched: {patched_text}"]
    return []


_GUARDS = denial_class_guards(_repo_root())


def test_enumeration_finds_both_guard_families():
    assert any(".write_guards." in g for g in _GUARDS)
    assert any(".bash_guards." in g for g in _GUARDS)


def test_patch_reaches_the_fleet_functions():
    names = {name for name, _ in _fleet_functions()}
    assert {"read_fleet_mode", "write_fleet_mode", "fleet_mode_path", "resolve_mode"} <= names
    with pytest.MonkeyPatch.context() as mp:
        assert _patch_fleet_reads(mp) >= len(names)
        fleet_mode = importlib.import_module("coordinator_core.session.fleet_mode")
        with pytest.raises(RuntimeError, match="fleet-mode read reached"):
            fleet_mode.read_fleet_mode()


def test_harness_flags_a_guard_that_reads_fleet_mode(tmp_path):
    def check(payload):
        from coordinator_core.session import fleet_mode

        fleet_mode.read_fleet_mode()
        return None

    failures = _independence_failures(
        SimpleNamespace(check=check), "synthetic", lambda root, mp: {}, tmp_path
    )
    assert failures and "fleet-mode read reached" in failures[0]


def test_harness_passes_a_guard_that_ignores_fleet_mode(tmp_path):
    guard = SimpleNamespace(check=lambda payload: {"hookSpecificOutput": {"permissionDecision": "deny"}})
    assert _independence_failures(guard, "synthetic", lambda root, mp: {}, tmp_path) == []


def test_is_deny_shapes():
    from coordinator_core.guard_messages.alternative_record import AlternativeRecord, Override
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    def record(kind: str) -> AlternativeRecord:
        return AlternativeRecord(
            verdict_kind=kind,
            summary="s",
            members=(AdaptableOffer(template="t", what_to_adapt="w"), Override("X")),
            guard_name="g",
            updated_input={"command": "c"} if kind == "rewrite" else None,
        )

    assert is_deny({"hookSpecificOutput": {"permissionDecision": "deny"}})
    assert is_deny(record("deny"))
    assert not is_deny(None)
    assert not is_deny(record("advisory"))
    assert not is_deny(record("rewrite"))
    assert not is_deny({"hookSpecificOutput": {"permissionDecision": "allow"}})


def test_no_stale_fixture_keys():
    assert sorted(set(ALL_FIXTURES) - set(_GUARDS)) == [], "fixtures for modules that are not denial-class guards"


def test_fixture_modules_do_not_overlap():
    assert len(ALL_FIXTURES) == len(write_guard_cases.FIXTURES) + len(bash_guard_cases.FIXTURES)


@pytest.mark.parametrize("dotted", _GUARDS)
def test_guard_verdicts_are_independent_of_fleet_mode(dotted, tmp_path):
    fixtures = ALL_FIXTURES.get(dotted)
    assert fixtures is not None, f"{dotted}: no fixtures (every denial-class guard needs a deny and an allow case)"
    assert fixtures["deny"], f"{dotted}: no deny case"
    assert fixtures["allow"], f"{dotted}: no allow case"

    guard = importlib.import_module(dotted)
    failures: list[str] = []
    cases = [("deny", i, c) for i, c in enumerate(fixtures["deny"])] + [
        ("allow", i, c) for i, c in enumerate(fixtures["allow"])
    ]
    for kind, index, case in cases:
        label = f"{dotted} {kind}[{index}]"
        verdict, _ = _run_case(guard, case, tmp_path / f"{kind}{index}" / "unpatched", patched=False)
        if isinstance(verdict, Exception):
            failures.append(f"{label}: guard raised unpatched: {verdict!r}")
            continue
        if is_deny(verdict) != (kind == "deny"):
            failures.append(f"{label}: expected {'a deny' if kind == 'deny' else 'no deny'}, got {verdict!r}")
            continue
        failures.extend(_independence_failures(guard, label, case, tmp_path / f"{kind}{index}"))
    assert not failures, "\n".join(failures)
