"""Ratchet: every engine module that builds a ``-m pytest`` argv is capped, exempted, or listed pending.

An AST scan with no spawn. A spawner is a non-test module under ``coordinator_core`` or
``coordinator/bin`` holding a list/tuple literal with the adjacent constants ``"-m", "pytest"``.
It must call ``spawn_in_capped_job`` or ``guarded_child_env`` (adopted), or sit in ``EXEMPT``
with a reason, or in ``PENDING_FIX`` with a follow-up row id. Anything else is a new uncapped
recursive-spawn site.

Trap: ``REQUIRED_ADOPTERS`` pins the modules that take their pytest argv from a caller (no
literal for the scan to see); they stay checked by name.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("coordinator_core", "coordinator/bin")
_SKIP_DIRS = frozenset({"tests", "fixtures", "__pycache__"})
_ADOPTION_CALLS = frozenset({"spawn_in_capped_job", "guarded_child_env"})

# module (repo-relative posix) -> why an uncapped pytest spawn there is safe.
EXEMPT: dict[str, str] = {
    "coordinator_core/benchmarks/spawn_guard_overhead.py": (
        "falsifier benchmark: launches inside its own Job Object (ActiveProcessLimit) with the "
        "guard env resolved dynamically, so it cannot call the adoption helpers by name"
    ),
}

# module -> follow-up row id that adopts the capped launcher. Not an exemption: a ratchet-down list.
# OPEN: C8-FU1 names no tracked row yet; until one exists under state/, these six are an untracked deferral.
PENDING_FIX: dict[str, str] = {
    "coordinator_core/ops/cutover_gate.py": "C8-FU1",
    "coordinator_core/ops/copy_plugin_template.py": "C8-FU1",
    "coordinator/bin/red-set-report.py": "C8-FU1",
    "coordinator/bin/stable-suite-run.py": "C8-FU1",
    "coordinator/bin/suite-verdict.py": "C8-FU1",
}

REQUIRED_ADOPTERS = (
    "coordinator_core/testing/scoped_pytest.py",
    "coordinator_core/source_edit_gate/runner.py",
    "coordinator_core/ops/dispatch_emit/retest_targeted.py",
    "coordinator_core/testing/run.py",
)


def _is_test_module(path: Path) -> bool:
    name = path.name
    return (
        name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or bool(_SKIP_DIRS.intersection(path.parts))
    )


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _builds_pytest_argv(tree: ast.AST) -> bool:
    compared = {
        id(n) for c in ast.walk(tree) if isinstance(c, ast.Compare) for n in (c.left, *c.comparators)
    }
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)) or id(node) in compared:
            continue
        vals = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
        if any(a == "-m" and b == "pytest" for a, b in zip(vals, vals[1:])):
            return True
    return False


def _calls_adoption(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
        if name in _ADOPTION_CALLS:
            return True
    return False


def scan(root: Path, scan_roots=SCAN_ROOTS) -> dict[str, bool]:
    """Repo-relative posix path -> adopted?, for every spawner module under the scan roots."""
    found: dict[str, bool] = {}
    for rel_root in scan_roots:
        base = root / rel_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if _is_test_module(path.relative_to(root)):
                continue
            try:
                if "pytest" not in path.read_text(encoding="utf-8", errors="replace"):
                    continue
            except OSError:
                continue
            tree = _parse(path)
            if tree is None or not _builds_pytest_argv(tree):
                continue
            found[path.relative_to(root).as_posix()] = _calls_adoption(tree)
    return found


def violations(found: dict[str, bool], exempt=None, pending=None) -> list[str]:
    exempt = EXEMPT if exempt is None else exempt
    pending = PENDING_FIX if pending is None else pending
    return sorted(m for m, adopted in found.items() if not adopted and m not in exempt and m not in pending)


@pytest.fixture(scope="module")
def census() -> dict[str, bool]:
    return scan(REPO_ROOT)


def test_every_pytest_spawner_is_capped_exempt_or_pending(census):
    assert not violations(census), (
        "uncapped `-m pytest` spawner(s); route through spawn_in_capped_job / guarded_child_env "
        "or add to EXEMPT (with a reason) or PENDING_FIX (with a follow-up row id)"
    )


def test_census_is_not_vacuous(census):
    assert len(census) >= 5, f"scan found {sorted(census)}; the detector has gone blind"


@pytest.mark.parametrize("module", REQUIRED_ADOPTERS)
def test_required_adopters_call_the_capped_launcher(module):
    tree = _parse(REPO_ROOT / module)
    assert tree is not None, f"{module} unreadable"
    assert _calls_adoption(tree), f"{module} no longer calls spawn_in_capped_job or guarded_child_env"


def test_exempt_and_pending_entries_are_live(census):
    for name, table in (("EXEMPT", EXEMPT), ("PENDING_FIX", PENDING_FIX)):
        for module, why in table.items():
            assert why.strip(), f"{name}[{module}] needs a non-empty reason or follow-up row id"
            assert module in census, f"{name}[{module}] is no longer a pytest spawner; delete the entry"
            assert not census[module], f"{name}[{module}] now adopts the capped launcher; delete the entry"
    assert not set(EXEMPT) & set(PENDING_FIX)


def test_child_env_chokepoint_caps_blas_pools_unless_set():
    from coordinator_core.ops._pytest_child_env import pytest_child_env
    from coordinator_core.testing import nested_session_guard, spawn_depth

    assert nested_session_guard.BLAS_THREAD_KEYS is spawn_depth.BLAS_THREAD_KEYS
    capped = spawn_depth.child_env({})
    assert all(capped[k] == "1" for k in spawn_depth.BLAS_THREAD_KEYS)
    assert spawn_depth.child_env({"MKL_NUM_THREADS": "4"})["MKL_NUM_THREADS"] == "4"
    assert all(pytest_child_env()[k] for k in spawn_depth.BLAS_THREAD_KEYS)


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


_UNCAPPED = "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pytest', 'x'])\n"


def test_fixture_uncapped_spawner_goes_red(tmp_path):
    _write(tmp_path, "coordinator_core/ops/bad.py", _UNCAPPED)
    found = scan(tmp_path)
    assert found == {"coordinator_core/ops/bad.py": False}
    assert violations(found, exempt={}, pending={}) == ["coordinator_core/ops/bad.py"]


def test_fixture_exempt_pending_and_adopted_are_admitted(tmp_path):
    _write(tmp_path, "coordinator_core/ops/bad.py", _UNCAPPED)
    found = scan(tmp_path)
    assert violations(found, exempt={"coordinator_core/ops/bad.py": "why"}, pending={}) == []
    assert violations(found, exempt={}, pending={"coordinator_core/ops/bad.py": "R1"}) == []
    _write(
        tmp_path,
        "coordinator_core/ops/good.py",
        "from x import spawn_in_capped_job\nspawn_in_capped_job(['python', '-m', 'pytest'])\n",
    )
    _write(
        tmp_path,
        "coordinator/bin/good2.py",
        "import m\nenv = m.guarded_child_env({})\nargv = ('-m', 'pytest')\n",
    )
    found = scan(tmp_path)
    assert found["coordinator_core/ops/good.py"] is True
    assert found["coordinator/bin/good2.py"] is True


def test_fixture_comparison_against_pytest_argv_is_not_a_spawn(tmp_path):
    _write(tmp_path, "coordinator_core/ops/cmp.py", "def f(t):\n    return t[:2] == ['-m', 'pytest']\n")
    assert scan(tmp_path) == {}


def test_fixture_test_modules_and_prose_are_not_spawners(tmp_path):
    _write(tmp_path, "coordinator_core/tests/test_a.py", _UNCAPPED)
    _write(tmp_path, "coordinator_core/ops/test_b.py", _UNCAPPED)
    _write(tmp_path, "coordinator_core/ops/doc.py", '"""runs [\'-m\', \'pytest\'] somewhere"""\nx = 1\n')
    assert scan(tmp_path) == {}
