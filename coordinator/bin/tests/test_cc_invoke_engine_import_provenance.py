"""test_cc_invoke_engine_import_provenance.py — AC for C2, "Test the query
against constructed sys.modules states, never ambient state".

Chunk: docs/plans/2026-08-26-the-seam-reports-what-it-got.md § C2

`cc_invoke.provenance_against(*, root)` (landed in C1) answers "where did the
ALREADY-IMPORTED `coordinator_core` actually come from, against a
caller-supplied root" without importing the engine itself and without ever
raising. Every case below constructs its own `sys.modules["coordinator_core"]`
stand-in (a bare `object()` fitted with just the attribute the case needs) and
restores whatever was there before — none of them reads this box's real
`coordinator_core` import state or its real engine roots, which is the whole
point: a test that asserted against ambient state would pass or fail
depending on which tree happened to be imported first in this pytest process,
never catching a genuine divergence.

Discharges AC2, AC3, AC4, AC5, AC6, AC11.

Run: pytest coordinator/bin/tests/test_cc_invoke_engine_import_provenance.py -q
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

pytestmark = pytest.mark.cadence


class _StandInModule:
    """A minimal stand-in for a `coordinator_core` module object — carries
    only `__file__` when a case wants it, never a real import."""

    def __init__(self, file_value=None, *, has_file=True):
        if has_file:
            self.__file__ = file_value


@pytest.fixture
def constructed_sys_modules():
    """Yields a setter that installs a stand-in at `sys.modules["coordinator_core"]`
    for the duration of one test, then restores the prior entry (present or
    absent) exactly — so no case leaks a fake engine import into a sibling
    test or into the real ambient state of this pytest process."""
    sentinel = object()
    prior = sys.modules.get("coordinator_core", sentinel)

    def _set(value):
        if value is sentinel:
            sys.modules.pop("coordinator_core", None)
        else:
            sys.modules["coordinator_core"] = value

    try:
        yield _set
    finally:
        if prior is sentinel:
            sys.modules.pop("coordinator_core", None)
        else:
            sys.modules["coordinator_core"] = prior


# ---------------------------------------------------------------------------
# AC2 — asking does not import the engine.
# ---------------------------------------------------------------------------


def test_unimported_engine_absent_yields_unimported_and_stays_absent(constructed_sys_modules):
    sys.modules.pop("coordinator_core", None)
    assert "coordinator_core" not in sys.modules

    result = _mod.provenance_against(root="/some/root")

    assert result.verdict == _mod.PROVENANCE_UNIMPORTED
    assert result.imported_file is None
    assert result.engine_root is None
    assert "coordinator_core" not in sys.modules, (
        "provenance_against must never import coordinator_core as a side "
        "effect of asking the question"
    )


# ---------------------------------------------------------------------------
# AC5 — verdict logic is correct both ways from one bound module.
# ---------------------------------------------------------------------------


def test_match_against_own_root_and_divergent_against_a_different_root(
    constructed_sys_modules, tmp_path
):
    own_root = tmp_path / "own-root"
    other_root = tmp_path / "other-root"
    own_root.mkdir()
    other_root.mkdir()
    imported_file = own_root / "coordinator_core" / "__init__.py"
    imported_file.parent.mkdir(parents=True)
    imported_file.write_text("", encoding="utf-8")

    constructed_sys_modules(_StandInModule(str(imported_file)))

    match_result = _mod.provenance_against(root=str(own_root))
    assert match_result.verdict == _mod.PROVENANCE_MATCH
    assert match_result.imported_file == str(imported_file.resolve())
    assert match_result.engine_root == str(own_root.resolve())

    divergent_result = _mod.provenance_against(root=str(other_root))
    assert divergent_result.verdict == _mod.PROVENANCE_DIVERGENT
    assert divergent_result.imported_file == str(imported_file.resolve())
    assert divergent_result.engine_root == str(other_root.resolve())


# ---------------------------------------------------------------------------
# AC4 — degrades, never guesses: no __file__ on the imported module.
# ---------------------------------------------------------------------------


def test_no_file_attribute_yields_unresolved(constructed_sys_modules):
    constructed_sys_modules(_StandInModule(has_file=False))

    result = _mod.provenance_against(root="/some/root")

    assert result.verdict == _mod.PROVENANCE_UNRESOLVED
    assert result.imported_file is None
    assert result.engine_root is None


# ---------------------------------------------------------------------------
# AC3 — never raises, on any input.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "root",
    [
        None,
        "",
        "/path/with/\x00-embedded-nul",
        123,
        object(),
        "/",
        "relative/path/no-leading-slash",
    ],
    ids=["none", "empty-string", "nul-embedded", "int", "bare-object", "root-slash", "relative"],
)
def test_hostile_root_inputs_never_raise(constructed_sys_modules, tmp_path, root):
    imported_file = tmp_path / "coordinator_core" / "__init__.py"
    imported_file.parent.mkdir(parents=True)
    imported_file.write_text("", encoding="utf-8")
    constructed_sys_modules(_StandInModule(str(imported_file)))

    result = _mod.provenance_against(root=root)

    assert result.verdict in (
        _mod.PROVENANCE_UNIMPORTED,
        _mod.PROVENANCE_MATCH,
        _mod.PROVENANCE_DIVERGENT,
        _mod.PROVENANCE_UNRESOLVED,
    )


def test_hostile_inputs_also_never_raise_when_module_absent(constructed_sys_modules):
    sys.modules.pop("coordinator_core", None)
    for root in (None, "", "/path/with/\x00-embedded-nul", 123, object(), "/", "relative/path"):
        result = _mod.provenance_against(root=root)
        assert result.verdict == _mod.PROVENANCE_UNIMPORTED


# ---------------------------------------------------------------------------
# AC11 — Stage 3 is not shipped: no raise/sys.exit/os._exit/assert anywhere
# in `provenance_against`'s own function body. AST-scoped over just this
# function's source, not a file-wide grep (the file contains `raise`
# elsewhere, e.g. its own outer `except Exception:` sibling functions).
# ---------------------------------------------------------------------------


def test_provenance_against_body_has_no_raise_exit_or_assert():
    source = textwrap.dedent(inspect.getsource(_mod.provenance_against))
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)

    forbidden_exit_calls = {"exit", "_exit"}
    for node in ast.walk(func_node):
        assert not isinstance(node, ast.Raise), (
            "provenance_against must never raise: found a Raise node in its "
            "own function body"
        )
        assert not isinstance(node, ast.Assert), (
            "provenance_against must never assert: found an Assert node in "
            "its own function body"
        )
        if isinstance(node, ast.Call):
            func = node.func
            called_name = None
            if isinstance(func, ast.Attribute):
                called_name = func.attr
            elif isinstance(func, ast.Name):
                called_name = func.id
            assert called_name not in forbidden_exit_calls, (
                f"provenance_against must never call sys.exit/os._exit: found "
                f"a call to {called_name!r}"
            )


# ---------------------------------------------------------------------------
# AC6 — no ambient-state dependence: this test module never resolves a real
# engine root and never reads COORDINATOR_ENGINE_ROOT. AST-based over this
# test module's own source (a literal-string grep for the two repo checkout
# names below is satisfiable by os.environ['COORDINATOR_ENGINE_ROOT'],
# resolve_engine_root(__file__), Path(__file__).parents[3], or importing the
# real coordinator_core and reading its __file__ — none of which contain
# either name, so only a call/read-shaped check catches them).
# ---------------------------------------------------------------------------

_AMBIENT_RESOLVER_NAMES = {
    "resolve_engine_root",
    "resolve_colocated_claude_klabauter_root",
    "_resolve_claude_klabauter_root",
}
# Built from parts, not written verbatim: a verbatim literal here would make
# this guard's own source contain the string it scans for, self-tripping the
# very assertion below.
_AMBIENT_REPO_LITERALS = (
    "-".join(["project", "claude-klabauter"]),
    "-".join(["claude", "klabauter"]),
)


def test_this_test_module_makes_no_ambient_resolver_call_or_env_read():
    own_source = Path(__file__).read_text(encoding="utf-8")

    for literal in _AMBIENT_REPO_LITERALS:
        assert literal not in own_source, (
            f"this test module must never hardcode the ambient repo literal "
            f"{literal!r} — every case must construct its own sys.modules "
            f"stand-in instead of reading this box's real engine roots"
        )

    tree = ast.parse(own_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called_name = None
            if isinstance(func, ast.Attribute):
                called_name = func.attr
            elif isinstance(func, ast.Name):
                called_name = func.id
            assert called_name not in _AMBIENT_RESOLVER_NAMES, (
                f"this test module must never call the ambient resolver "
                f"{called_name!r} — every case must construct its own "
                f"sys.modules stand-in instead"
            )
        if isinstance(node, ast.Subscript):
            value = node.value
            is_os_environ = (
                isinstance(value, ast.Attribute)
                and value.attr == "environ"
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
            )
            if is_os_environ:
                key_node = node.slice
                if isinstance(key_node, ast.Constant) and key_node.value == "COORDINATOR_ENGINE_ROOT":
                    pytest.fail(
                        "this test module must never read "
                        "os.environ['COORDINATOR_ENGINE_ROOT'] — every case "
                        "must construct its own sys.modules stand-in instead"
                    )
