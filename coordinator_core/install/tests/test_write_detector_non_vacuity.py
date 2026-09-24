"""
coordinator_core.install.tests.test_write_detector_non_vacuity — AC3's
non-vacuity harness for the inverted write-detector
(``test_write_reaching_modules_declare.py``).

Spec: docs/plans/2026-09-11-invert-the-write-detector-prove-read-only.md (P075-C3)
Arm: ARM B (per-branch fixture table), per
docs/research/spike-verdicts/2026-09-11-write-detector-mutation-harness-viability.md
(verdict: not-viable — the in-process AST mutation harness is correct but its
process-time budget, 1.13-1.27s across three measured runs, is 2.2-2.7x the
500ms ceiling, and C2's post-inversion file is materially larger, so
re-measuring would only move it further over budget, not under it).

ARM B does not re-verify revert-and-go-red on future edits of the detector
file — that property belongs only to ARM A, which the C1 verdict rejected on
budget. This harness instead enumerates every If/IfExp branch site inside a
module-level, non-test, non-test-infrastructure function of the detector
module (``_TEST_INFRA_HELPERS`` names the excluded test-only helpers the C1
spike found being swept in by a blanket "every module-level non-test
function" rule: ``_find_open_fdopen_arm``, ``_make_call``, ``_flagged_names``
are used only by ``test_``-prefixed callers and are not part of the
detector's predicate surface), and requires a ``_BRANCH_FIXTURES`` entry for
every one of them. Each entry runs the REAL detector function (loaded from
its file path below, never imported by package name) against a concrete
input chosen so that this branch's outcome determines an observable result,
and asserts the real result equals the recorded expectation.

One-time hand revert (AC3's ARM B close-out requirement — an authoring-time
record, not a runtime check): each of the 63 enumerated sites' ``ast.If``/
``ast.IfExp`` test was negated once (``ast.UnaryOp(Not, ...)`` over the
site's test, the module recompiled and exec'd into a fresh namespace, never
written to disk), and its mapped ``_BRANCH_FIXTURES`` probe re-run against
that mutated namespace. 60/63 negations produced a value that disagreed
with the recorded expectation — each is a real witness for its site, not a
vacuous one. 3 sites are EQUIVALENT under negation for any input (recorded
here, not fixed, per the same "classify by reading" standard the C1 verdict
used for its own survivors):

  - ``_p2_flags_are_read_only`` / ``flags_arg is None``: when ``flags_arg``
    is ``None``, negating the early-``return False`` falls through to
    ``_all_read(None)``, whose ``isinstance`` checks both fail for ``None``
    too — the fallthrough path converges on the same ``False`` the early
    return would have given, for every possible caller.
  - ``_proves_read_only`` / ``receiver_name is None``: negating the early
    ``return False`` lets execution reach the ``receiver_name in
    io_names``/``os_names`` membership tests with ``receiver_name is
    None`` — ``None`` is never a member of either frozenset, so every
    later branch still resolves to the same ``False``.
  - ``_module_verdicts`` / ``not hits``: for a module with no flagged
    calls, negating the early ``continue`` lets execution reach
    ``any(... for hit in hits)`` (vacuously ``False`` over an empty
    sequence) and ``_open_family_hits_excused`` (vacuously ``True`` over no
    open/fdopen hits) — both give the same "not unexplained" result the
    early continue would have given.

Negative spec (ARM B's stated gap, per AC3): this harness does not
re-verify revert-and-go-red on a FUTURE edit to the detector file the way
ARM A would — it verifies, once, that every currently-enumerated branch has
a witness (or is a recorded equivalent). A branch added by a future edit
with no fixture is caught by ``test_every_branch_site_has_a_fixture``
(coverage), but a future edit that changes a branch's OUTCOME without
changing its site key is not automatically re-negated and re-checked here;
ARM A's revert-and-go-red property is what would close that gap, and the
C1 verdict rejected it on budget alone, not on correctness.

Measured (this test module, whole run, ``time.process_time()``,
``pytest -q -p no:cacheprovider``, no ``-n``): well under the 500ms/zero-
spawn budget — the harness does no ``subprocess``/shell work anywhere; every
fixture is in-process AST parsing plus direct calls into the loaded
detector module.
"""

from __future__ import annotations

import ast
import importlib.util
import tempfile
import types
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_DETECTOR_PATH = _THIS_DIR / "test_write_reaching_modules_declare.py"

_TEST_INFRA_HELPERS = frozenset({"_find_open_fdopen_arm", "_make_call", "_flagged_names"})
"""Module-level, non-``test_``-prefixed helpers in the detector file that
exist only to serve a ``test_``-prefixed caller, per the C1 spike's own
scoping finding: a blanket "every module-level non-test function" targeting
rule sweeps these in even though they are test infrastructure, not part of
the detector's predicate surface."""


def _load_detector() -> types.ModuleType:
    """Load the detector module from its file path, never by package name
    (which would shadow a mutated/exec'd copy elsewhere in this file)."""
    spec = importlib.util.spec_from_file_location(
        "write_detector_non_vacuity_target", _DETECTOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DETECTOR = _load_detector()
_DETECTOR_SOURCE = _DETECTOR_PATH.read_text(encoding="utf-8")


def _enumerate_branch_sites(source: str) -> list[tuple[str, str]]:
    """Every (function name, ``ast.unparse`` of the test expression) pair for
    an ``If``/``IfExp`` inside a module-level, non-``test_``, non-test-
    infrastructure function of the detector module. This is ARM B's coverage
    unit, per the C3 row body ("Enumerate branch sites from the detector's
    AST, keyed by (function qualname, ``ast.unparse`` of the test
    expression)")."""
    tree = ast.parse(source)
    sites: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("test_") or node.name in _TEST_INFRA_HELPERS:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, (ast.If, ast.IfExp)):
                sites.append((node.name, ast.unparse(inner.test)))
    return sites


def _missing_fixture_keys(
    sites: list[tuple[str, str]], fixtures: dict[tuple[str, str], Any]
) -> list[tuple[str, str]]:
    """Every enumerated site with no ``_BRANCH_FIXTURES`` entry — the
    coverage check's failure mode."""
    return [key for key in sites if key not in fixtures]


def _stale_fixture_keys(
    sites: list[tuple[str, str]], fixtures: dict[tuple[str, str], Any]
) -> list[tuple[str, str]]:
    """Every ``_BRANCH_FIXTURES`` entry that no longer names a live site —
    the stale-check half of "every entry names a live site"."""
    live = set(sites)
    return [key for key in fixtures if key not in live]


# ---------------------------------------------------------------------------
# Fixture-construction helpers. Each fixture calls the REAL detector
# function loaded above against a concrete, minimal input chosen so this
# one branch's outcome determines the result.
# ---------------------------------------------------------------------------


def _parse_call(source: str) -> tuple[ast.Call, frozenset[str], frozenset[str]]:
    tree = ast.parse(source)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    os_names = frozenset(_DETECTOR._module_imported_as(tree, "os"))
    io_names = frozenset(_DETECTOR._module_imported_as(tree, "io"))
    return call, os_names, io_names


def _build_branch_fixtures(tmp_dir: Path) -> dict[tuple[str, str], tuple[bool, bool]]:
    """Return {(function, test-expr): (expected, actual)} for every fixture.
    Built once, eagerly, against real inputs and the real (unmutated)
    detector module."""
    D = _DETECTOR
    fixtures: dict[tuple[str, str], tuple[bool, bool]] = {}

    def add(func: str, expr: str, expected: bool, actual: bool) -> None:
        fixtures[(func, expr)] = (expected, actual)

    def tmp_module(name: str, source: str) -> Path:
        p = tmp_dir / name
        p.write_text(source, encoding="utf-8")
        return p

    # --- _is_str_replace_false_positive ---
    call, _os, _io = _parse_call("x.replace('a')\n")
    add(
        "_is_str_replace_false_positive",
        "len(call.args) != 2",
        False,
        D._is_str_replace_false_positive(call),
    )

    # --- _subprocess_imported_names ---
    tree = ast.parse("from subprocess import run\n")
    add(
        "_subprocess_imported_names",
        "isinstance(node, ast.ImportFrom) and node.module == 'subprocess'",
        True,
        "run" in D._subprocess_imported_names(tree),
    )

    # --- _module_imported_as ---
    tree = ast.parse("import os\n")
    result = D._module_imported_as(tree, "os")
    add("_module_imported_as", "isinstance(node, ast.Import)", True, "os" in result)
    add("_module_imported_as", "alias.name == module_name", True, "os" in result)

    # --- _from_import_bound_names ---
    tree = ast.parse("from gzip import open\n")
    result = D._from_import_bound_names(tree, frozenset({"open", "fdopen"}))
    add("_from_import_bound_names", "isinstance(node, ast.ImportFrom)", True, "open" in result)
    add("_from_import_bound_names", "alias.name in target_names", True, "open" in result)

    # --- _bare_open_fdopen_names_rebound ---
    tree = ast.parse("from gzip import open\n")
    result = D._bare_open_fdopen_names_rebound(tree)
    add("_bare_open_fdopen_names_rebound", "isinstance(node, ast.ImportFrom)", True, "open" in result)
    add("_bare_open_fdopen_names_rebound", "bound in targets", True, "open" in result)

    # --- _qualname_for_scope ---
    add(
        "_qualname_for_scope",
        "not any((kind == 'func' for kind, _name in scope))",
        True,
        D._qualname_for_scope(()) == "<module>",
    )

    # --- _walk_calls ---
    src = "class C:\n    def m(self):\n        return open(p)\n"
    tree = ast.parse(src)
    seen: list[tuple[str, str]] = []
    D._walk_calls(tree, (), lambda node, qn: seen.append((getattr(node.func, "id", getattr(node.func, "attr", None)), qn)))
    walked_ok = seen == [("open", "C.m")]
    add("_walk_calls", "isinstance(node, ast.Call)", True, walked_ok)
    add("_walk_calls", "isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))", True, walked_ok)
    add("_walk_calls", "isinstance(node, ast.ClassDef)", True, walked_ok)

    # --- _flagged_calls ---
    def flagged_names(source: str) -> set[str]:
        p = tmp_module("flagged_probe.py", source)
        return {hit.name for hit in D._flagged_calls(p)}

    add(
        "_flagged_calls",
        "isinstance(func, ast.Attribute)",
        True,
        "open" in flagged_names("p.open(x)\n"),
    )
    add(
        "_flagged_calls",
        "name == 'replace'",
        True,
        "replace" in flagged_names("def f(a, b):\n    return x.replace(a, b)\n"),
    )
    add(
        "_flagged_calls",
        "name in ('open', 'fdopen')",
        True,
        "open" in flagged_names("p.open(x)\n"),
    )
    add(
        "_flagged_calls",
        "not is_attribute_call and name in open_alias_names",
        True,
        "o" in flagged_names("from io import open as o\ndef f(p):\n    return o(p, 'w')\n"),
    )
    add(
        "_flagged_calls",
        "name in _SUBPROCESS_NAMES",
        True,
        "run" in flagged_names("import subprocess\ndef f():\n    return subprocess.run(x)\n"),
    )
    add(
        "_flagged_calls",
        "name in _MUTATING_ATTRS",
        True,
        "write_text" in flagged_names("def f(p):\n    return p.write_text(x)\n"),
    )
    add(
        "_flagged_calls",
        "isinstance(func, ast.Name)",
        True,
        "o" in flagged_names("from io import open as o\ndef f(p):\n    return o(p, 'w')\n"),
    )
    add(
        "_flagged_calls",
        "_is_str_replace_false_positive(node)",
        True,
        "replace" in flagged_names("def f(a, b):\n    return x.replace(a, b)\n"),
    )
    add(
        "_flagged_calls",
        "is_attribute_call or name in subprocess_imported_names",
        True,
        "run" in flagged_names("from subprocess import run\ndef f():\n    return run(x)\n"),
    )

    # --- _declares_write_surface ---
    p_assign = tmp_module("declares_assign.py", "WRITE_SURFACE = []\n")
    p_ann = tmp_module("declares_ann.py", "WRITE_SURFACE: list = []\n")
    add("_declares_write_surface", "isinstance(node, ast.Assign)", True, D._declares_write_surface(p_assign))
    add(
        "_declares_write_surface",
        "isinstance(target, ast.Name) and target.id == 'WRITE_SURFACE'",
        True,
        D._declares_write_surface(p_assign),
    )
    add(
        "_declares_write_surface",
        "isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)",
        True,
        D._declares_write_surface(p_ann),
    )
    add(
        "_declares_write_surface",
        "node.target.id == 'WRITE_SURFACE'",
        True,
        D._declares_write_surface(p_ann),
    )

    # --- _p1_mode_is_read_only ---
    def p1(source: str) -> bool:
        call, _os, _io = _parse_call(source)
        return D._p1_mode_is_read_only(call, mode_index=1)

    add("_p1_mode_is_read_only", "len(call.args) > mode_index", False, p1("open(p, 'w')\n"))
    add("_p1_mode_is_read_only", "mode_arg is None", True, p1("open(p)\n"))
    add(
        "_p1_mode_is_read_only",
        "not (isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str))",
        False,
        p1("open(p, 5)\n"),
    )
    add("_p1_mode_is_read_only", "not mode_arg.value", False, p1("open(p, '')\n"))
    add("_p1_mode_is_read_only", "kw.arg == 'mode'", False, p1("open(p, mode='w')\n"))

    # --- _p2_flags_are_read_only ---
    def p2(source: str) -> bool:
        call, os_names, _io = _parse_call(source)
        return D._p2_flags_are_read_only(call, os_names)

    add("_p2_flags_are_read_only", "len(call.args) >= 2", True, p2("import os\nos.open(p, os.O_RDONLY)\n"))
    add("_p2_flags_are_read_only", "flags_arg is None", False, p2("import os\nos.open(p)\n"))
    add(
        "_p2_flags_are_read_only",
        "isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)",
        True,
        p2("import os\nos.open(p, os.O_RDONLY | os.O_NONBLOCK)\n"),
    )
    add(
        "_p2_flags_are_read_only",
        "isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)",
        True,
        p2("import os\nos.open(p, os.O_RDONLY)\n"),
    )
    add("_p2_flags_are_read_only", "kw.arg == 'flags'", True, p2("import os\nos.open(p, flags=os.O_RDONLY)\n"))
    add(
        "_p2_flags_are_read_only",
        "node.value.id not in os_names",
        False,
        p2("import os\nos.open(p, foo.O_RDONLY)\n"),
    )

    # --- _proves_read_only ---
    def prove(source: str) -> bool:
        call, os_names, io_names = _parse_call(source)
        return D._proves_read_only(call, os_names, io_names)

    add("_proves_read_only", "any((isinstance(arg, ast.Starred) for arg in call.args))", False, prove("open(*args)\n"))
    add("_proves_read_only", "any((kw.arg is None for kw in call.keywords))", False, prove("open(p, **kw)\n"))
    add(
        "_proves_read_only",
        "any((kw.arg == 'opener' for kw in call.keywords))",
        False,
        prove("open(p, 'r', opener=make_writer)\n"),
    )
    add(
        "_proves_read_only",
        "len(call.args) > 7",
        False,
        prove("open(p, 'r', -1, True, None, None, None, make_writer)\n"),
    )
    add("_proves_read_only", "isinstance(func, ast.Name)", True, prove("open(p)\n"))
    add("_proves_read_only", "isinstance(func, ast.Attribute)", True, prove("import io\nio.open(p, 'r')\n"))
    add("_proves_read_only", "func.id in ('open', 'fdopen')", False, prove("foo(p)\n"))
    add("_proves_read_only", "receiver_name is None", False, prove("foo().open('r')\n"))
    add(
        "_proves_read_only",
        "func.attr == 'open' and receiver_name in io_names",
        True,
        prove("import io\nio.open(p, 'r')\n"),
    )
    add(
        "_proves_read_only",
        "func.attr == 'fdopen' and receiver_name in os_names",
        True,
        prove("import os\nos.fdopen(fd, 'r')\n"),
    )
    add(
        "_proves_read_only",
        "func.attr == 'open' and receiver_name in os_names",
        True,
        prove("import os\nos.open(p, os.O_RDONLY)\n"),
    )
    add(
        "_proves_read_only",
        "isinstance(func.value, ast.Name)",
        True,
        prove("import os\nos.open(p, os.O_RDONLY)\n"),
    )

    # --- _open_family_hits_excused ---
    p_non_open = tmp_module("excused_non_open.py", "def f(p):\n    p.write_text('x')\n")
    add(
        "_open_family_hits_excused",
        "hit.name not in ('open', 'fdopen')",
        True,
        D._open_family_hits_excused(p_non_open, {}),
    )
    p_no_entry = tmp_module("excused_no_entry.py", "def f(p):\n    return open(p, 'w')\n")
    add(
        "_open_family_hits_excused",
        "key not in read_only_opens",
        False,
        D._open_family_hits_excused(p_no_entry, {}),
    )
    p_rebound = tmp_module("excused_rebound.py", "from gzip import open\ndef f(p):\n    return open(p)\n")
    rebound_key = (p_rebound.name, "f", "open")
    add(
        "_open_family_hits_excused",
        "isinstance(func, ast.Name) and func.id in bare_rebound",
        False,
        D._open_family_hits_excused(p_rebound, {rebound_key: "reasoned, but rebound"}),
    )
    write_key = (p_no_entry.name, "f", "open")
    add(
        "_open_family_hits_excused",
        "not _proves_read_only(hit.node, os_names, io_names)",
        False,
        D._open_family_hits_excused(p_no_entry, {write_key: "reasoned, but wrong"}),
    )

    # --- _module_verdicts ---
    p_empty = tmp_module("verdicts_empty.py", "x = 1\n")
    add("_module_verdicts", "not hits", True, D._module_verdicts([p_empty], {}, {}) == [])
    p_declares = tmp_module(
        "verdicts_declares.py", "WRITE_SURFACE = []\ndef f(p):\n    p.write_text('x')\n"
    )
    add(
        "_module_verdicts",
        "_declares_write_surface(module_path)",
        True,
        D._module_verdicts([p_declares], {}, {}) == [],
    )
    p_allowlisted = tmp_module("verdicts_allowlisted.py", "def f(p):\n    p.write_text('x')\n")
    add(
        "_module_verdicts",
        "module_path.name in allowlist",
        True,
        D._module_verdicts([p_allowlisted], {}, {p_allowlisted.name: "reason"}) == [],
    )
    p_nonopen = tmp_module("verdicts_nonopen.py", "def f(p):\n    p.write_text('x')\n")
    add(
        "_module_verdicts",
        "any((hit.name not in ('open', 'fdopen') for hit in hits))",
        True,
        D._module_verdicts([p_nonopen], {}, {}) == [p_nonopen.name],
    )
    p_openwrite = tmp_module("verdicts_openwrite.py", "def f(p):\n    return open(p, 'w')\n")
    add(
        "_module_verdicts",
        "not _open_family_hits_excused(module_path, read_only_opens)",
        True,
        D._module_verdicts([p_openwrite], {}, {}) == [p_openwrite.name],
    )

    # --- _read_only_opens_staleness ---
    stale, disproven = D._read_only_opens_staleness({("gone.py", "f", "open"): "r"}, tmp_dir, [])
    add(
        "_read_only_opens_staleness",
        "module_name not in current_names",
        True,
        any("no longer exists" in s for s in stale),
    )
    p_site = tmp_module("staleness_site.py", "def f(p):\n    return open(p)\n")
    stale, disproven = D._read_only_opens_staleness({(p_site.name, "g", "open"): "r"}, tmp_dir, [p_site])
    add("_read_only_opens_staleness", "not hits", True, any("site no longer flagged" in s for s in stale))
    p_now_declares = tmp_module(
        "staleness_declares.py", "WRITE_SURFACE = []\ndef f(p):\n    return open(p)\n"
    )
    stale, disproven = D._read_only_opens_staleness(
        {(p_now_declares.name, "f", "open"): "r"}, tmp_dir, [p_now_declares]
    )
    add(
        "_read_only_opens_staleness",
        "_declares_write_surface(module_path)",
        True,
        any("now declares" in s for s in stale),
    )
    p_now_allow = tmp_module("staleness_allow.py", "def f(p):\n    return open(p)\n")
    stale, disproven = D._read_only_opens_staleness(
        {(p_now_allow.name, "f", "open"): "r"},
        tmp_dir,
        [p_now_allow],
        allowlist={p_now_allow.name: "r"},
    )
    add(
        "_read_only_opens_staleness",
        "module_name in allowlist",
        True,
        any("allowlisted" in s for s in stale),
    )
    p_stale_rebound = tmp_module(
        "staleness_rebound.py", "from gzip import open\ndef f(p):\n    return open(p)\n"
    )
    stale, disproven = D._read_only_opens_staleness(
        {(p_stale_rebound.name, "f", "open"): "r"}, tmp_dir, [p_stale_rebound]
    )
    add(
        "_read_only_opens_staleness",
        "isinstance(func, ast.Name) and func.id in bare_rebound",
        True,
        any("now rebound" in d for d in disproven),
    )
    p_stale_disproven = tmp_module("staleness_disproven.py", "def f(p):\n    return open(p, 'w')\n")
    stale, disproven = D._read_only_opens_staleness(
        {(p_stale_disproven.name, "f", "open"): "r"}, tmp_dir, [p_stale_disproven]
    )
    add(
        "_read_only_opens_staleness",
        "not _proves_read_only(hit.node, os_names, io_names)",
        True,
        any("no longer proves" in d for d in disproven),
    )

    return fixtures


def _fixture_dir() -> Path:
    global _FIXTURE_TMPDIR
    _FIXTURE_TMPDIR = tempfile.TemporaryDirectory()
    return Path(_FIXTURE_TMPDIR.name)


_FIXTURE_TMPDIR: tempfile.TemporaryDirectory | None = None
_BRANCH_FIXTURES: dict[tuple[str, str], tuple[bool, bool]] = _build_branch_fixtures(_fixture_dir())


def test_every_branch_site_has_a_fixture() -> None:
    """Coverage: every enumerated If/IfExp site in a targeted production
    function must carry a ``_BRANCH_FIXTURES`` entry."""
    sites = _enumerate_branch_sites(_DETECTOR_SOURCE)
    missing = _missing_fixture_keys(sites, _BRANCH_FIXTURES)
    assert not missing, f"branch site(s) with no _BRANCH_FIXTURES entry: {missing}"


def test_every_fixture_names_a_live_site() -> None:
    """Stale check: every ``_BRANCH_FIXTURES`` key must still name an
    enumerated site (mirrors ``_ALLOWLIST``'s own stale-entry self-check)."""
    sites = _enumerate_branch_sites(_DETECTOR_SOURCE)
    stale = _stale_fixture_keys(sites, _BRANCH_FIXTURES)
    assert not stale, f"_BRANCH_FIXTURES entry names no live site: {stale}"


def test_every_branch_fixture_produces_expected_outcome() -> None:
    """Each fixture's real result (computed once, eagerly, against the
    unmutated detector module) must match its recorded expectation."""
    wrong = [key for key, (expected, actual) in _BRANCH_FIXTURES.items() if expected != actual]
    assert not wrong, f"fixture produced an unexpected outcome at: {wrong}"


def test_coverage_check_reports_planted_uncovered_branch() -> None:
    """Self-test: a synthetic site with no fixture entry must be reported
    missing by the coverage check — proves the check itself is not vacuous."""
    sites = _enumerate_branch_sites(_DETECTOR_SOURCE)
    planted = ("some_planted_function", "planted_condition_no_one_wrote")
    assert planted not in _BRANCH_FIXTURES
    missing = _missing_fixture_keys(sites + [planted], _BRANCH_FIXTURES)
    assert planted in missing


def test_stale_check_reports_planted_dead_entry() -> None:
    """Self-test's stale-side twin: a fixture entry naming a site that does
    not exist must be reported by the stale check."""
    sites = _enumerate_branch_sites(_DETECTOR_SOURCE)
    planted_key = ("nonexistent_function", "nonexistent_condition")
    fixtures_with_dead_entry = dict(_BRANCH_FIXTURES)
    fixtures_with_dead_entry[planted_key] = (True, True)
    stale = _stale_fixture_keys(sites, fixtures_with_dead_entry)
    assert planted_key in stale


def test_no_duplicate_branch_site_keys() -> None:
    """Structural safety net: two distinct branches in the same function
    that happen to ``ast.unparse`` identically would silently collapse to
    one dict key. None do today; this test fails loud the day one does."""
    sites = _enumerate_branch_sites(_DETECTOR_SOURCE)
    assert len(sites) == len(set(sites)), "duplicate (function, test-expr) branch site key(s) found"
