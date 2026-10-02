"""
coordinator_core.tests.test_settings_home_is_never_bound_once — the once-bound-reader guard.

Purpose: under a resident warm server, a caller that binds `settings_home()` (or an aliased
import of it) exactly once — at import time, at def time, as a class/dataclass attribute
default, or via lazy module-global memoization — freezes whichever caller's environment the
server happened to be serving at that instant, and then hands that frozen value to every later
caller regardless of its own `COORDINATOR_SETTINGS_HOME`. Only a per-call read is correct on
this seam (`state/dispatch-briefs/2026-08-31-the-settings-home-crosses-the-warm-boundary/C4.md`).

This module is an AST walk, not an import: it never imports the modules it inspects (a resident
server importing arbitrary coordinator_core submodules to audit them would itself defeat the
purpose), and it never spawns a subprocess. `ast.parse` over source text only.

Negative-spec: a per-call read — `def f(): return settings_home() / "x"` inside a function body,
called fresh each time — is exactly the correct shape and MUST NOT be flagged. The walk targets
only the four shapes enumerated below; it is not a general "don't use settings_home" lint.
"""

from __future__ import annotations

import ast
import re
from bisect import bisect_right
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "coordinator_core"

_TARGET_CALL_NAMES = {
    "settings_home",
    # DERIVED READERS COUNT TOO. A caller that never names `settings_home`
    # still freezes it by binding something computed FROM it -- measured live:
    # `ops/emit/doe_drift.py` bound `machine_local_dir() / "registry.local.toml"`
    # at module scope and served the first importing request's registry to every
    # later caller, while this sweep read green. A guard that matches only the
    # literal name selects its blind spot by the same property the defect has.
    "machine_local_dir",
    "settings_home_child_env",
}


def _bound_local_names(tree: ast.Module) -> set[str]:
    """Local names in this module that are (possibly aliased) imports of settings_home."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _TARGET_CALL_NAMES:
                    names.add(alias.asname or alias.name)
    return names | _TARGET_CALL_NAMES


def _is_target_call(node: ast.AST, bound_names: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in bound_names
    if isinstance(func, ast.Attribute):
        return func.attr in bound_names
    return False


def _contains_target_call(node: ast.AST, bound_names: set[str]) -> bool:
    for sub in ast.walk(node):
        if _is_target_call(sub, bound_names):
            return True
    return False


def _decorator_is_cache(dec: ast.AST) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id in ("lru_cache", "cache")
    if isinstance(target, ast.Attribute):
        return target.attr in ("lru_cache", "cache")
    return False


def _module_scope_names(tree: ast.Module) -> set[str]:
    """Names that genuinely live at module scope -- assigned at the top level,
    or declared `global` somewhere in the file.

    Shape 5 (the lazily-populated cache) is only a defect for a name that
    OUTLIVES the call. A function parameter defaulting to `None` and resolved
    per call inside the body is the CORRECT shape, and reads identically to the
    defect at the AST node the walk visits -- `root_channel_reconcile.py ::
    reconcile_root`'s `machine_local` parameter is the measured case. Without
    this filter the guard flags the fix it exists to recommend.
    """
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    return names


def find_violations(source: str, filename: str = "<string>") -> list[str]:
    """Return a list of human-readable violation strings, empty if none found."""
    tree = ast.parse(source, filename=filename)
    return _violations_in_tree(tree, filename, _module_scope_names(tree))


def _violations_in_tree(
    tree: ast.Module, filename: str, module_scope_names: set[str] | None
) -> list[str]:
    """`module_scope_names=None` treats every name as module-scope: a superset, so
    it can only over-flag shape 5 -- the chunked sweep re-verifies any hit."""
    bound_names = _bound_local_names(tree)
    violations: list[str] = []

    # A single pass over every node. Shapes that would otherwise need a
    # nested `ast.walk` per function (and so cost O(functions * body size)
    # on a file with many small functions) are resolved directly against
    # the node the outer walk is already visiting.
    for node in ast.walk(tree):
        # Shape 1: @lru_cache / @cache on a function whose body calls settings_home().
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_decorator_is_cache(d) for d in node.decorator_list):
                if _contains_target_call(node, bound_names):
                    violations.append(
                        f"{filename}:{node.lineno}: @lru_cache/@cache reader {node.name!r} "
                        "memoizes settings_home() across callers"
                    )

            # Shape 2: mutable default arg evaluated once at def time.
            for default in list(node.args.defaults) + list(node.args.kw_defaults):
                if default is not None and _is_target_call(default, bound_names):
                    violations.append(
                        f"{filename}:{node.lineno}: def {node.name!r} binds a default "
                        "argument to settings_home() at def time"
                    )

        # Shape 5: lazily-populated module/enclosing global. The outer walk
        # already visits every `if` node regardless of nesting depth, so no
        # per-function re-walk of the body is needed here.
        if isinstance(node, ast.If):
            test = node.test
            is_none_check = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Is)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            )
            if is_none_check and (
                module_scope_names is None or test.left.id in module_scope_names
            ):
                cache_name = test.left.id
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and any(
                            isinstance(t, ast.Name) and t.id == cache_name
                            for t in stmt.targets
                        )
                        and _is_target_call(stmt.value, bound_names)
                    ):
                        violations.append(
                            f"{filename}:{node.lineno}: lazily-populated global "
                            f"{cache_name!r} is bound to settings_home() once"
                        )

        # Shape 3: class attribute / dataclass field default.
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    if _is_target_call(stmt.value, bound_names):
                        violations.append(
                            f"{filename}:{stmt.lineno}: class {node.name!r} binds a field "
                            "default to settings_home() once"
                        )
                elif isinstance(stmt, ast.Assign):
                    if _is_target_call(stmt.value, bound_names):
                        violations.append(
                            f"{filename}:{stmt.lineno}: class {node.name!r} binds a class "
                            "attribute to settings_home() once"
                        )

    # Shape 4: module-level assignment (direct, or a Path(...) composed from it).
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            value = stmt.value
            if value is not None and _contains_target_call(value, bound_names):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                names = ", ".join(
                    t.id for t in targets if isinstance(t, ast.Name)
                )
                violations.append(
                    f"{filename}:{stmt.lineno}: module-level binding {names!r} composes "
                    "settings_home() once at import time"
                )

    return violations


# ---------------------------------------------------------------------------
# Chunked sweep. Parsing is the whole cost of the live walk (~5MB of source),
# and a violation can only sit in a top-level statement that textually names a
# target call, so only those statements are parsed; every other chunk is blanked
# to its newlines, keeping line numbers true. Trap: a chunk starts at any
# column-0 line outside a triple-quoted string that is not a closer, comment
# or backslash continuation (a decorator joins the statement it decorates), so a
# statement hand-formatted with a column-0 continuation line inside brackets
# splits wrongly -- the repo's formatting never produces one. A chunk that fails
# to parse, and any hit, fall back to the full-file walk, so the chunked path
# can only skip work, never invent or hide a verdict for a well-formed file.
# ---------------------------------------------------------------------------

_TRIPLE_QUOTE = re.compile(r"\"\"\"|'''")
_AS_ALIAS = re.compile(r"\s+as\s+(\w+)")


def _triple_quoted_spans(source: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while m := _TRIPLE_QUOTE.search(source, pos):
        close = source.find(m.group(), m.end())
        pos = len(source) if close < 0 else close + 3
        spans.append((m.start(), pos))
    return spans


def _name_hits(source: str, names: set[str]) -> list[int]:
    # Substring hits over-match (`settings_home` inside a longer name), which only
    # keeps more chunks; plain str.find is far cheaper than a regex alternation.
    hits: list[int] = []
    for name in names:
        i = source.find(name)
        while i >= 0:
            hits.append(i)
            i = source.find(name, i + len(name))
    return hits


def _keep_only_chunks_naming_targets(source: str) -> str:
    hits = _name_hits(source, _TARGET_CALL_NAMES)
    aliases = {
        a.group(1)
        for i in hits
        for name in _TARGET_CALL_NAMES
        if source.startswith(name, i) and (a := _AS_ALIAS.match(source, i + len(name)))
    }
    hits += _name_hits(source, aliases)
    end = len(source)
    strings = _triple_quoted_spans(source)
    string_starts = [lo for lo, _ in strings]

    def in_string(i: int) -> bool:
        # A line start inside a triple-quoted string is text, not a statement.
        k = bisect_right(string_starts, i - 1) - 1
        return k >= 0 and i < strings[k][1]

    def starts_chunk(i: int) -> bool:
        return (
            i == 0
            or i == end
            or (
                not in_string(i)
                and source[i] not in " \t\r\n#)]}"
                and source[i - 2 : i] != "\\\n"
            )
        )

    def prev_start(i: int) -> int:
        while i > 0:
            i = source.rfind("\n", 0, i - 1) + 1
            if starts_chunk(i):
                break
        return i

    def next_start(i: int) -> int:
        while i < end:
            nl = source.find("\n", i)
            i = end if nl < 0 else nl + 1
            if starts_chunk(i):
                break
        return i

    def chunk_around(pos: int) -> tuple[int, int]:
        lo = source.rfind("\n", 0, pos) + 1
        if not starts_chunk(lo):
            lo = prev_start(lo)
        # A decorator belongs to the statement it decorates.
        while lo > 0 and source[prev_start(lo)] == "@":
            lo = prev_start(lo)
        head, hi = lo, next_start(lo)
        while source[head] == "@" and hi < end:
            head, hi = hi, next_start(hi)
        return lo, hi

    spans = sorted({chunk_around(i) for i in hits})
    out: list[str] = []
    done = 0
    for lo, hi in spans:
        if lo < done:
            continue
        out.append("\n" * source.count("\n", done, lo))
        out.append(source[lo:hi])
        done = hi
    out.append("\n" * source.count("\n", done))
    return "".join(out)


def find_violations_chunked(source: str, filename: str = "<string>") -> list[str]:
    """`find_violations` with the same verdict, parsing only the chunks that name a target."""
    try:
        tree = ast.parse(_keep_only_chunks_naming_targets(source), filename=filename)
    except SyntaxError:
        return find_violations(source, filename)
    if _violations_in_tree(tree, filename, None):
        return find_violations(source, filename)
    return []


# ---------------------------------------------------------------------------
# Discriminating red leg: one inline source string per shape the walk claims
# to catch, plus a clean control. No fixture module on disk.
# ---------------------------------------------------------------------------

_LRU_CACHE_READER = """
from functools import lru_cache
from coordinator_core._settings_home import settings_home

@lru_cache(maxsize=None)
def cached_home():
    return settings_home()
"""

_MUTABLE_DEFAULT_ARG = """
from coordinator_core._settings_home import settings_home

def resolve(home=settings_home()):
    return home
"""

_CLASS_ATTRIBUTE_DEFAULT = """
from coordinator_core._settings_home import settings_home

class Config:
    home: "Path" = settings_home()
"""

_LAZY_MODULE_GLOBAL = """
from coordinator_core._settings_home import settings_home

_CACHE = None

def cached_home():
    global _CACHE
    if _CACHE is None:
        _CACHE = settings_home()
    return _CACHE
"""

_CLEAN_PER_CALL = """
from coordinator_core._settings_home import settings_home

def resolve():
    return settings_home() / "x"

class Config:
    def home(self):
        return settings_home()
"""

_CLEAN_MODULE_LEVEL_CONSTANT_UNRELATED = """
from pathlib import Path

DEFAULT = Path("x")
"""


def test_flags_lru_cache_reader():
    violations = find_violations(_LRU_CACHE_READER, "lru_cache_reader.py")
    assert violations, "lru_cache-wrapped settings_home() reader must be flagged"
    assert any("cached_home" in v for v in violations)


def test_flags_mutable_default_argument():
    violations = find_violations(_MUTABLE_DEFAULT_ARG, "mutable_default_arg.py")
    assert violations, "settings_home() bound as a default argument must be flagged"
    assert any("resolve" in v for v in violations)


def test_flags_class_attribute_default():
    violations = find_violations(_CLASS_ATTRIBUTE_DEFAULT, "class_attribute_default.py")
    assert violations, "settings_home() bound as a class attribute default must be flagged"
    assert any("Config" in v for v in violations)


def test_flags_lazily_populated_module_global():
    violations = find_violations(_LAZY_MODULE_GLOBAL, "lazy_module_global.py")
    assert violations, "lazily-populated module global caching settings_home() must be flagged"
    assert any("_CACHE" in v for v in violations)


def test_does_not_flag_clean_per_call_reads():
    violations = find_violations(_CLEAN_PER_CALL, "clean_per_call.py")
    assert violations == [], f"per-call reads must not be flagged, got: {violations}"

    violations = find_violations(
        _CLEAN_MODULE_LEVEL_CONSTANT_UNRELATED, "clean_unrelated_constant.py"
    )
    assert violations == [], f"unrelated module constant must not be flagged, got: {violations}"


# ---------------------------------------------------------------------------
# Live sweep over coordinator_core/** non-test files. Import-free, no
# subprocess. Process time is asserted under the 500ms brightline so the
# sweep itself never becomes a suppression candidate.
# ---------------------------------------------------------------------------


BRIGHTLINE_MS = 500


def _non_test_py_files() -> list[Path]:
    files = []
    for path in CORE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(CORE_ROOT)
        if "tests" in rel.parts:
            continue
        if any(part.startswith("test_") or part.endswith("_test.py") for part in rel.parts):
            continue
        files.append(path)
    return files


def test_no_live_once_bound_settings_home_reader_in_coordinator_core():
    # Directory enumeration and disk reads are filesystem cost, not the AST
    # walk's own cost -- read every candidate source once, outside the timer,
    # then measure only ast.parse + node-walk over the in-memory sources.
    sources: list[tuple[Path, str]] = []
    for path in _non_test_py_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(name in text for name in _TARGET_CALL_NAMES):
            sources.append((path, text))

    from coordinator_core.benchmarks.process_time import in_process_time_ms

    outcome: dict = {}

    def _walk() -> None:
        examined = 0
        violations: list[str] = []
        for path, source in sources:
            examined += 1
            try:
                violations.extend(
                    find_violations_chunked(source, str(path.relative_to(REPO_ROOT)))
                )
            except SyntaxError:
                continue
        outcome["examined"] = examined
        outcome["violations"] = violations

    timing = in_process_time_ms(_walk)
    examined = outcome["examined"]
    all_violations = outcome["violations"]
    elapsed_ms = timing["process_time_ms"]

    assert elapsed_ms < BRIGHTLINE_MS, (
        f"AST walk over {examined} target-referencing files took {elapsed_ms:.1f}ms "
        f"of process time, over the {BRIGHTLINE_MS}ms brightline"
    )
    assert examined > 0, "expected at least one file referencing settings_home to examine"
    assert all_violations == [], (
        "found once-bound settings_home() reader(s), needs a per-call fix:\n"
        + "\n".join(all_violations)
    )


# ---------------------------------------------------------------------------
# DERIVED-READER LEGS. Added after an adversarial criterion-only read of HEAD
# found a live freeze this sweep had reported clean: the literal-name matcher
# saw no `settings_home` in `ops/emit/doe_drift.py` and so never looked at
# `machine_local_dir() / "registry.local.toml"` bound at module scope. A guard
# whose blind spot has the same shape as the defect is the failure this file
# exists to prevent, so both directions are pinned here.
# ---------------------------------------------------------------------------

_DERIVED_MODULE_LEVEL = '''
from coordinator_core._settings_home import machine_local_dir
_REGISTRY_LOCAL = machine_local_dir() / "registry.local.toml"
'''

_DERIVED_LAZY_GLOBAL = '''
from coordinator_core._settings_home import machine_local_dir
_CACHE = None
def get():
    global _CACHE
    if _CACHE is None:
        _CACHE = machine_local_dir()
    return _CACHE
'''

_PER_CALL_PARAMETER_CONTROL = '''
from coordinator_core._settings_home import machine_local_dir
def reconcile(machine_local=None):
    if machine_local is None:
        machine_local = machine_local_dir()
    return machine_local
'''


def test_a_derived_reader_bound_at_module_scope_is_flagged():
    """The measured live instance, in its pre-fix shape. `machine_local_dir()`
    never names `settings_home`, and freezes it just the same."""
    violations = find_violations(_DERIVED_MODULE_LEVEL, "doe_drift.py")

    assert violations, "a module-level binding composed from a DERIVED reader escaped the sweep"


def test_a_derived_reader_cached_in_a_module_global_is_flagged():
    violations = find_violations(_DERIVED_LAZY_GLOBAL, "x.py")

    assert violations, "a lazily-populated global holding a derived reader escaped the sweep"


def test_a_per_call_parameter_default_is_not_flagged():
    """The precision control, and the reason it is here: a parameter defaulting
    to `None` and resolved per call INSIDE the body is the correct shape -- the
    very fix this guard recommends. It reads identically to the lazy-global
    defect at the `ast.If` node the walk visits, and an unfiltered matcher
    flagged `root_channel_reconcile.py :: reconcile_root` for having taken the
    advice."""
    violations = find_violations(_PER_CALL_PARAMETER_CONTROL, "x.py")

    assert violations == [], f"flagged the correct per-call shape: {violations}"


# ---------------------------------------------------------------------------
# CHUNKED-PATH PARITY. The sweep skips parsing, so every fixture above must get
# the same verdict from the chunked walk, and the shapes only a chunk split
# could get wrong are pinned here.
# ---------------------------------------------------------------------------

_ALIASED_MODULE_LEVEL = '''
from coordinator_core._settings_home import (
    settings_home as sh,
)

def per_call():
    return sh()

_FROZEN = sh() / "x"
'''

_COLUMN_ZERO_TEXT_IN_DOCSTRING = '''
"""Notes.
_FROZEN = settings_home()
"""
from coordinator_core._settings_home import settings_home

def per_call():
    return settings_home()
'''


def test_chunked_walk_agrees_with_the_full_walk_on_every_fixture():
    fixtures = [
        _LRU_CACHE_READER,
        _MUTABLE_DEFAULT_ARG,
        _CLASS_ATTRIBUTE_DEFAULT,
        _LAZY_MODULE_GLOBAL,
        _CLEAN_PER_CALL,
        _CLEAN_MODULE_LEVEL_CONSTANT_UNRELATED,
        _DERIVED_MODULE_LEVEL,
        _DERIVED_LAZY_GLOBAL,
        _PER_CALL_PARAMETER_CONTROL,
        _ALIASED_MODULE_LEVEL,
        _COLUMN_ZERO_TEXT_IN_DOCSTRING,
    ]
    for source in fixtures:
        assert find_violations_chunked(source, "f.py") == find_violations(source, "f.py")


def test_chunked_walk_flags_an_aliased_module_level_binding_on_its_true_line():
    violations = find_violations_chunked(_ALIASED_MODULE_LEVEL, "aliased.py")

    assert len(violations) == 1 and violations[0].startswith("aliased.py:9:")


def test_chunked_walk_does_not_flag_column_zero_text_inside_a_docstring():
    assert find_violations_chunked(_COLUMN_ZERO_TEXT_IN_DOCSTRING, "doc.py") == []
