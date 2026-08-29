"""
coordinator_core.tests.test_no_hardcoded_paths — C0b gate: no hardcoded
absolute/drive-anchored paths, and no `__file__`/`parents[n]` (or
`os.path.dirname(...)`-chain) directory traversal that crosses a repo
boundary, in coordinator_core/. Scope is PER-TOOTH as of 2026-07-25 (see
§ Scope decision below): the drive-anchored-literal tooth stays
production-only, the sibling-crossing-traversal tooth also covers
`test_*.py` files.

Context: the 2026-07-22 fence-inventory buildout (docs/plans/2026-07-22-
coordinator-ops-buildout-from-fence-inventory.md § C0b, DEC-4) mandates this
gate as the mechanized enforcement of constraint (3) ("no hardcoded paths").
The live violation this gate was scoped to catch:
`coordinator_core/frontmatter/schema_drift_watch.py::resolve_doe_repo_path`
used to walk `Path(__file__).resolve().parents[2]` to derive "claude-klabauter repo
root", then guessed `claude_klabauter_root.parent / "DoE-claude"` as the sibling
clone's location — hardcoding both the checkout depth from this file AND a
flat-sibling directory layout. Retired 2026-07-22 in the same change that
added this gate; the fix delegates entirely to
`coordinator_core.doe_root_pointer.read_doe_root_pointer()` (registry-first,
DR-071), which resolves the DoE root without any assumption about checkout
layout. See that module's docstring for the retirement negative-spec.

Two independently-triggered teeth, per DEC-4's "teeth-tightening" of the
plan's original two-tooth description (structured as a direct sibling of
`coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py` — same
exemption-set shape, same by-(file, symbol)-tuple granularity, same
plant-a-violation test discipline, same AST-not-regex mechanism so comments
and docstrings are structurally excluded rather than pattern-excluded):

  Tooth 1 — ROOT/DRIVE-ANCHORED CONSTRUCTION. A `Path(...)` or
  `os.path.join(...)` call whose first argument is a string-literal absolute
  POSIX path (leading `/`, not a single `"/"`) or a Windows drive-letter
  path (`C:\\...` / `C:/...`). Catches a hardcoded absolute path regardless
  of whether it also names a sibling repo.

  Tooth 2 — SIBLING-REPO-BOUNDARY-CROSSING TRAVERSAL. A path-construction
  expression (a `/`-chain of `Path` operations, or an `os.path.join(...)`
  call) whose root traces back — directly, or through a simple same-module
  variable assignment — to a `__file__`-anchored directory climb (`.parents`
  / `.parent` attribute access, or an `os.path.dirname(...)` call wrapping
  `__file__`), where the SAME expression's string-constant set also contains
  a NAMED sibling-repo token (`_SIBLING_REPO_TOKENS` below). In-repo climbing
  alone (`parents[n]` with no sibling-repo token anywhere in the
  construction) is explicitly fine and must not be flagged — the hazard is
  specifically climbing out of the repo to GUESS a sibling checkout's
  location, not climbing within it.

Scope decision (DEC-4 offered two choices; this gate took the first,
PRODUCTION CODE ONLY, at authoring time — **amended 2026-07-25, PM-authorized,
toward the second option, PER-TOOTH rather than uniformly.** The initial
2026-07-25 pass widened `_is_excluded_source_path` to stop skipping
`test_*.py` files outright for BOTH teeth, then measured the fallout before
committing to that shape (per the authorizing brief's own step-2/step-3
split) — the measurement showed the two teeth do not carry the same risk in
test code, so the landed scope refines rather than uniformly executes the
authorization: **Tooth 2 (sibling-repo-crossing-traversal) is IN SCOPE for
`test_*.py` files; Tooth 1 (root-or-drive-anchored-literal) STAYS
PRODUCTION-ONLY.** `_is_excluded_source_path` still skips non-test files
(`conftest.py`, helpers, fixture data) under a `tests/` directory outright —
neither tooth applies to those, unchanged from day one — while `test_*.py`
files ARE now parsed, with Tooth 1 findings filtered out for them at
`find_hardcoded_path_violations`'s per-tooth check.

Rationale for widening Tooth 2 into test code: the original
day-one-8+-pre-existing-instances tradeoff (recorded below, kept for
history) was a bet that the test-file exclusion was low-risk for BOTH
teeth — three real Tooth-2-shaped instances surfaced on 2026-07-25 alone,
all in test code, none catchable by the gate as originally scoped:
`coordinator_core/tests/test_step_zero_emit.py` and
`coordinator_core/tests/test_normalize_snippet.py` (both fixed in
`9057a88c`), and `_write_doe_root_sentinel` in
`coordinator/bin/test_cross_repo_memo.py`, whose `Path(__file__).parents[3]`
landed back inside claude-klabauter instead of at the sibling root, so
`identity.redirectAliases` never resolved and three assertions had been
passing against nothing (fixed in `3e37ba3b`). That last one is the
decisive case: in test code, this exact anti-pattern degrades a test into
silently-asserting-nothing rather than into a visible failure — the same
defect class the 2026-07-25 286-inert-tests cleanup exists to catch — so
Tooth 2's day-one "none of these are silent-wrong-path bugs" premise no
longer holds for that tooth specifically.

Rationale for keeping Tooth 1 production-only: the 2026-07-25 fallout
measurement (running the naively-widened, both-teeth scan before landing
the per-tooth split) found ZERO new Tooth 2 hits and 45 unique Tooth 1 hits
in test code, of which 43 were ordinary mock/placeholder literals
(`/tmp/...`, `/fake/...`, `X:/DoE-claude`, etc.) handed as arguments to
functions under test — not portability defects, just fixture data. A
root-anchored literal is the hazardous SHAPE only when it's a real
resolution the running code depends on; as a mock input to a function being
exercised, it carries none of Tooth 1's risk, and widening Tooth 1 into
tests would have meant either accepting the 43-entry noise permanently or
growing `_EXEMPT_SITES` past the point DEC-4's naming discipline can absorb
on a single amendment. The other 2 of the 45 WERE adjudicated individually
rather than bulk-deferred: `test_sentinel.py`'s
`test_resolve_claude_home_does_not_double_suffix_when_env_already_ends_in_dot_claude`
hardcoded a real username path (`/Users/example-operator/.claude`) as its example
input where any placeholder would do — fixed directly to
`/fake/home/.claude`, no gate exemption needed since it's simply no longer a
real-looking path. `test_settings_home.py`'s
`test_normalize_native_path_is_noop_on_posix` pairs a root literal with the
`DoE-claude` token but is a pure string-mount-form fixture for
`normalize_native_path` (mirroring the sibling msys/cygdrive tests' use of
the same `"/x/DoE-claude"` literal) with no `__file__` climb anywhere in
reach — adjudicated as a genuine fixture, not a sibling-resolution site, and
left as-is.

Original day-one rationale (2026-07-22), kept for history: a `git grep`
sweep at authoring time found 8+ pre-existing instances of this exact shape
already living in test files — e.g.
`coordinator/tests/test_run_report_fold_cli_roundtrip.py`'s
`CLAUDE_KLABAUTER_ROOT = REPO_ROOT.parent / "claude-klabauter"` — believed at the time to
be same-repo dev-convenience sibling-checkout probes guarded by
`pytest.mark.skipif`, not production paths that silently degrade.
Mass-rewriting 8+ files in the same pass was out of scope for that change;
bringing tests in immediately would have meant either rewriting all of them
at once or growing `_EXEMPT_SITES` to 8+ entries on day one, defeating DEC-4's
"a site is exempt because it is named, never because it fits a rationale"
discipline before the gate had caught a single real bug. Test-file coverage
was named as a tracked follow-up then, not a silent gap — this paragraph is
that follow-up landing.

Unrelated to the test-file scope cut: the `coordinator/` tree at the claude-klabauter
repo root (a ported coordinator-claude bash-test-suite migration, unrelated
to this plan's 64 ops) stays out of this gate's scan root entirely —
`_SCAN_ROOT` is `coordinator_core/` only, mirroring
`test_no_node_schema_shellout.py`'s own `_SCAN_ROOT`. The
`coordinator/bin/test_cross_repo_memo.py` instance above was fixed directly
(it lives outside `_SCAN_ROOT`) but is not, and will not become, gate-covered
by this amendment — the amendment widens what `_is_excluded_source_path`
scans WITHIN `_SCAN_ROOT`, it does not widen `_SCAN_ROOT` itself.

Negative-spec:
  - Does NOT flag in-repo-only `parents[n]` climbing with no sibling-repo
    token in the same construction (e.g. `Path(__file__).resolve().parents[2]`
    used alone to find the claude-klabauter repo root, or
    `coordinator_core/ops/test_probe_cwd_example_retrieval_repo_relevance.py`'s own
    `parents[2]` climb — a `test_*.py` file, so Tooth 2 DOES see it since the
    2026-07-25 amendment, but it carries no sibling-repo token in the same
    construction, so it stays unflagged on the merits, not on scope).
  - Does NOT flag doc/comment/docstring mentions of the hazardous shape
    (e.g. this module's own docstring, or `schema_drift_watch.py`'s
    retirement negative-spec prose) — AST-based, so a `#` comment or a bare
    string-literal statement is never materialized as a `Call`/`BinOp` node
    and cannot trip either tooth no matter what text it contains.
  - Does NOT do full interprocedural/cross-module dataflow — same-module,
    same-pass, forward-only Name-to-taint tracking only (sufficient for the
    actual production violation this gate was built to catch, which was a
    two-statement same-function same-module split:
    `claude_klabauter_root = Path(__file__).resolve().parents[2]` then
    `claude_klabauter_root.parent / "DoE-claude"`). A violation split across module
    boundaries, or reconstructed through a function call, is a false
    negative this gate accepts — same class of accepted tradeoff
    `test_no_node_schema_shellout.py`'s docstring names for dynamic string
    construction.
  - Exemption entries are `module::symbol` strings (function name, or
    `<module>` for module-level code) per DEC-4 — enumerated because named,
    never because they fit a rationale. See `_EXEMPT_SITES` below.

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-
inventory.md § C0b, DEC-4.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

# Named sibling-repo tokens this gate treats as a repo-boundary crossing when
# they co-occur, in one path-construction expression, with a __file__-anchored
# directory climb. Deliberately a closed, small list — not "every repo name
# ever seen" — per the dispatch brief's minimum set plus the plan's own seed
# set (DEC-4 names DoE-claude and claude-klabauter; .claude is the third anchor
# CLAUDE.md § Runtime conventions and trusted_root_guard.py both treat as a
# trust/resolution boundary).
_SIBLING_REPO_TOKENS = {"DoE-claude", "claude-klabauter", "example-retrieval-repo", ".claude"}

_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:[\\/]")

# Exemption set: `module::symbol` strings, `module` relative to _REPO_ROOT
# with forward slashes, `symbol` the enclosing function name or `<module>`.
# Named because named (DEC-4) -- adding an entry is a plan amendment, not an
# executor call. Seed set derived at authoring time (2026-07-22):
#   - pyresolve.py's Windows install-probe literals (`os.path.join("C:\\Program
#     Files", ...)` / `os.path.join("C:\\", ...)`) inside `_pyorg_search` --
#     genuine Windows-native absolute-path probing, not a portability hazard;
#     the literal IS the platform-specific target being searched for.
#   - lifecycle.py's POSIX-only `Path("/tmp")` service-socket base inside
#     `global_sentinel_dir`'s `hasattr(os, "getuid")` branch -- guarded to the
#     POSIX branch specifically, with the Windows branch using
#     `tempfile.gettempdir()` instead (see that function's own comment).
_EXEMPT_SITES: set[str] = {
    "coordinator_core/pyresolve.py::_pyorg_search",
    "coordinator_core/lifecycle.py::global_sentinel_dir",
    # explicit -> REPO_DOE_CLAUDE env -> sibling-dir fallback (mirrors
    # doe_drift's resolution ladder); the sibling probe is the last rung,
    # never the sole source of truth. 2026-08-25.
    "coordinator_core/contract/cockpit_schema/emit_conformance_fixture.py::resolve_doe_clone",
    # POSIX `sh` discovery ladder (shutil.which -> /bin/sh existence probe ->
    # git-for-windows sibling layout); `/bin/sh` names a real, portable
    # convention path, not a repo-local hardcode. 2026-08-25.
    "coordinator_core/testing/sh_interpreter.py::_resolve_sh_interpreter",
    # Tooth 3. Locates the published klabauter engine, which genuinely IS a
    # sibling of this repo -- the flat-sibling layout is the thing under test,
    # not an accident. Consults the engine-root override env var first and only
    # then falls back to the sibling guess; the fallback is what trips Tooth 3,
    # and it reads, never creates. 2026-08-29.
    "coordinator_core/benchmarks/tests/test_warm_door_process_time_gate.py::_candidate_source_roots",
    # System-gitconfig discovery ladder: git-for-windows sibling layouts
    # derived from `git`'s own bin dir, then `/etc/gitconfig` as the last
    # rung. That literal names git's POSIX system-config convention, which is
    # the same path on every POSIX host -- it is not a repo-local or
    # machine-local hardcode, and there is no registry key that could resolve
    # it. Same shape as `sh_interpreter`'s `/bin/sh` rung above. 2026-08-29.
    "coordinator_core/git/content_hash.py::_system_gitconfig_paths",
}


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_")


def _is_excluded_source_path(path: Path) -> bool:
    """File-level scope predicate (see module docstring § Scope decision).
    Amended 2026-07-25: `test_*.py` files are no longer skipped outright —
    they're now parsed so Tooth 2 can see them (per-tooth scope, see
    `find_hardcoded_path_violations`'s tooth filter). Non-test files under a
    `tests/` directory (`conftest.py`, helpers, fixture data) remain skipped
    entirely; that narrower cut wasn't the shape the 2026-07-25 amendment
    targeted and widening it further is a separate call."""
    return "tests" in path.parts and not _is_test_file(path)


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _is_dunder_file(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "__file__"


def _contains_dunder_file(node: ast.AST) -> bool:
    return any(_is_dunder_file(n) for n in ast.walk(node))


def _is_os_path_dirname_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "dirname"
    if isinstance(func, ast.Name):
        return func.id == "dirname"
    return False


def _contains_climb_marker(node: ast.AST) -> bool:
    """True if `node` contains a `.parents`/`.parent` attribute access, or a
    `os.path.dirname(...)` call -- the two directory-climb shapes this repo
    uses, per constraint (3)'s stated hazard (`parents[N]` and the
    `os.path.dirname(...)` x N variant)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in ("parents", "parent"):
            return True
        if _is_os_path_dirname_call(n):
            return True
    return False


def _is_climb_tainted_expr(node: ast.AST) -> bool:
    """A __file__-anchored directory climb: both a climb marker AND a
    reference to __file__ in the same expression subtree."""
    return _contains_climb_marker(node) and _contains_dunder_file(node)


#: Returned as the level count for a climb that lands at the anchoring file's
#: filesystem root -- `parents[-1]`. The exact number of levels is unknowable
#: statically (it depends where the checkout lives), but the DESTINATION is
#: known exactly: the top of the drive, which is above every repo root there
#: can be. A sentinel larger than any real path depth makes that destination
#: compare correctly against any `file_depth` without inventing a fake count.
_CLIMB_TO_FILESYSTEM_ROOT = 10**6


def _is_dunder_file_anchor(node: ast.AST) -> bool:
    """The BARE `__file__` name only.

    NEGATIVE SPEC -- `<module>.__file__` (an ast.Attribute) is deliberately
    NOT an anchor. Tooth 3 decides "above the repo root" by comparing a climb
    against the depth of the file being SCANNED, and that arithmetic only
    holds when the climb starts at that same file. A climb rooted at another
    module's `__file__` -- `test_engine_root_conformance.py` walks from a
    module it loaded out of the DoE-claude checkout -- starts somewhere this
    gate cannot measure, so counting its levels against the scanned file's
    depth reports an escape that is not one. Unmeasurable is `None`, never a
    guess. Binding it to a local first (`_FILE = __file__`) is still traced:
    see `bindings` on `_climb_levels_above_file`.
    """
    return isinstance(node, ast.Name) and node.id == "__file__"


def _parents_index_levels(index: ast.expr) -> int | None:
    """Levels contributed by the subscript of a `.parents[...]` access.

    `parents[k]` is `k + 1` levels above the anchor (`parents[0]` is already
    the containing directory). `parents[-1]` is the filesystem root, the
    single most extreme climb expressible, and parses as `UnaryOp(USub,
    Constant)` rather than a `Constant` -- the shape a naive `isinstance(...,
    ast.Constant)` check misses precisely where it matters most.

    NEGATIVE SPEC -- a non-literal subscript (`parents[i]`, `parents[n - 1]`)
    returns `None`. No amount of further AST matching closes that one; it is
    the accepted blind spot of a static tooth, named here rather than left
    implicit.
    """
    if isinstance(index, ast.Constant) and isinstance(index.value, int):
        return index.value + 1 if index.value >= 0 else _CLIMB_TO_FILESYSTEM_ROOT
    if (
        isinstance(index, ast.UnaryOp)
        and isinstance(index.op, ast.USub)
        and isinstance(index.operand, ast.Constant)
        and isinstance(index.operand.value, int)
    ):
        return _CLIMB_TO_FILESYSTEM_ROOT
    return None


def _segment_delta(nodes: list[ast.expr]) -> int | None:
    """Net level change from appending path segments, or `None` if unknowable.

    `".."` climbs one, `"."` moves nothing, and ANY other literal segment
    DESCENDS one. That last rule is the one worth stating: a climb followed by
    a descent is the overwhelmingly common shape in this repo --
    `Path(__file__).resolve().parents[3] / "coordinator" / "bin"` reaches the
    repo root and then walks back down into it, and is not an escape. Counting
    only the climb and ignoring the descent reports every such expression as
    leaving the repo, which is how the first cut of this helper produced a
    dozen false positives.

    A non-literal segment (`root / name`) makes the destination unknowable, so
    the whole expression yields `None` rather than a guess -- consistent with
    every other unresolvable shape in this tooth.
    """
    delta = 0
    for n in nodes:
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            return None
        if n.value == "..":
            delta += 1
        elif n.value not in (".", ""):
            delta -= 1
    return delta


def _climb_levels_above_file(node: ast.AST, bindings: dict[str, int] | None = None) -> int | None:
    """How many directory levels above the anchoring file `node` resolves to,
    or `None` when `node` is not a `__file__`-anchored climb at all.

    `Path(__file__)` is 0 (the file itself), `.parent` adds one, `.parents[k]`
    adds `k + 1`, an `os.path.dirname(...)` wrap adds one, a `".."` segment
    adds one, and any other literal segment SUBTRACTS one (a descent back down
    -- see `_segment_delta`). `Path(...)`/`.resolve()`/`.absolute()` are transparent
    wrappers that move nothing. Anything else returns `None` rather than a
    guess -- an unrecognized shape must not be reported as an escape.

    `bindings` maps a local name to the climb it already resolves to, so a
    chain broken across statements (`p = Path(__file__).resolve()` then
    `q = p.parents[3].parent`) is traced rather than silently dropped at the
    variable. Aliasing is an ordinary refactor, not an exotic evasion, and
    without this the tooth goes blind the moment anyone does it.
    """
    bindings = bindings or {}
    if _is_dunder_file_anchor(node):
        return 0
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Call):
        func_name = _resolved_func_name(node.func)
        if func_name in ("resolve", "absolute") and isinstance(node.func, ast.Attribute):
            return _climb_levels_above_file(node.func.value, bindings)
        if func_name == "Path" and len(node.args) == 1:
            return _climb_levels_above_file(node.args[0], bindings)
        if func_name == "dirname" and _is_os_path_dirname_call(node) and len(node.args) == 1:
            inner = _climb_levels_above_file(node.args[0], bindings)
            return None if inner is None else inner + 1
        if func_name in ("joinpath", "join") and isinstance(node.func, ast.Attribute):
            inner = _climb_levels_above_file(node.func.value, bindings)
            delta = _segment_delta(node.args)
            return None if inner is None or delta is None else inner + delta
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        inner = _climb_levels_above_file(node.left, bindings)
        delta = _segment_delta([node.right])
        return None if inner is None or delta is None else inner + delta
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = _climb_levels_above_file(node.value, bindings)
        return None if inner is None else inner + 1
    if isinstance(node, ast.Subscript):
        base = node.value
        if isinstance(base, ast.Attribute) and base.attr == "parents":
            levels = _parents_index_levels(node.slice)
            if levels is None:
                return None
            inner = _climb_levels_above_file(base.value, bindings)
            return None if inner is None else inner + levels
    return None


def _string_constants(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _names_referenced(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _matched_sibling_tokens(strings: list[str]) -> set[str]:
    return {s for s in strings if s in _SIBLING_REPO_TOKENS}


def _is_root_or_drive_anchored_literal(value: str) -> bool:
    if value == "/":
        return False
    if value.startswith("/"):
        return True
    return bool(_DRIVE_LETTER_RE.match(value))


def _resolved_func_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _root_anchored_literal_arg(call: ast.Call, *, all_positions: bool) -> ast.Constant | None:
    """Return the first positional arg that's a root/drive-anchored string
    literal, or None.

    ``all_positions=False`` (``Path(...)``): only ``args[0]`` is inspected —
    only ``Path``'s first positional arg establishes the root; later args
    are ordinary path segments regardless of content.

    ``all_positions=True`` (``os.path.join``/``joinpath``): EVERY positional
    arg is inspected — ``os.path.join``'s actual semantics mean any
    absolute-path argument, at any position, resets the join and discards
    everything before it, so a hardcoded literal in position 2+ is exactly
    the hazard this tooth exists to catch, not just position 0."""
    args = call.args if all_positions else call.args[:1]
    for arg in args:
        if (
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and _is_root_or_drive_anchored_literal(arg.value)
        ):
            return arg
    return None


class _HardcodedPathVisitor(ast.NodeVisitor):
    """Single forward pass over one module: tracks same-module,
    same-pass Name-to-climb-taint, then flags Tooth 1 (root/drive-anchored
    literal construction) and Tooth 2 (sibling-repo-crossing traversal)
    violations as they're encountered, in source order."""

    def __init__(self, enclosing_fn: str = "<module>", file_depth: int | None = None) -> None:
        self.violations: list[tuple[int, str, str, str]] = []  # (lineno, tooth, symbol, detail)
        self._tainted_names: set[str] = set()
        self._fn_stack: list[str] = [enclosing_fn]
        #: Path components between the repo root and this file inclusive, so a
        #: climb of exactly `file_depth` lands ON the repo root and anything
        #: greater lands above it. `None` disables Tooth 3 (a caller scanning
        #: a file whose position relative to a repo root is unknown).
        self._file_depth = file_depth
        #: (lineno, symbol) -> greatest climb seen. Every inner node of a
        #: chain also parses as a shorter climb, so the outermost -- the one
        #: that actually decides where the expression lands -- is the max.
        self._climbs: dict[tuple[int, str], int] = {}
        #: Local name -> the climb depth its RHS already resolves to, so a
        #: chain broken across statements stays traceable. Function-scoped
        #: exactly like `_tainted_names`, and for the same reason: two
        #: unrelated functions reusing a name must not bleed into each other.
        self._climb_bindings: dict[str, int] = {}

    @property
    def _symbol(self) -> str:
        return self._fn_stack[-1]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Snapshot-and-restore _tainted_names on function entry/exit: taint
        # is same-module, same-pass, but must stay scoped to ONE function's
        # locals — otherwise a later, unrelated function whose parameter or
        # local happens to share a tainted name from an EARLIER function
        # (e.g. both use `root`) is falsely flagged as climb-tainted purely
        # from name reuse, not from any actual __file__-climb relationship.
        outer_tainted = self._tainted_names
        outer_bindings = self._climb_bindings
        self._tainted_names = set()
        self._climb_bindings = dict(outer_bindings)
        self._fn_stack.append(node.name)
        self.generic_visit(node)
        self._fn_stack.pop()
        self._tainted_names = outer_tainted
        self._climb_bindings = outer_bindings

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        # NOTE: no explicit self._check_construction(node.value) call here
        # (fixed 2026-07-25 — it double-counted every Assign whose RHS was
        # itself a Call/BinOp(Div): generic_visit(node) below already visits
        # node.value as a direct child field and dispatches it through
        # visit_Call/visit_BinOp, which independently call
        # _check_construction on the SAME node. The explicit call produced
        # an exact-duplicate violation tuple for every such Assign; taint
        # tracking below is unaffected since it inspects node.value directly
        # rather than depending on the (now-removed) construction check.
        #
        # Taint propagates two ways: (1) the RHS carries a direct climb
        # marker itself (the original single-hop case), or (2) the RHS
        # merely REFERENCES an already-tainted name -- e.g. `claude_klabauter_root =
        # os.path.abspath(os.path.join(here, "..", ".."))` where `here` was
        # tainted by an earlier statement and `claude_klabauter_root`'s own RHS has no
        # climb marker of its own. Without (2), a same-function multi-hop
        # chain (climb -> intermediate var -> final sibling-token join)
        # loses the taint at the intermediate hop and the gate goes blind
        # past one variable.
        if _is_climb_tainted_expr(node.value) or bool(self._tainted_names & _names_referenced(node.value)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._tainted_names.add(target.id)
        self.generic_visit(node)
        # Bind AFTER the RHS has been visited: the assignment's own value is
        # recorded as a climb in its own right by visit_Attribute/Subscript,
        # and only then does the target name become an alias later statements
        # can chain off.
        levels = _climb_levels_above_file(node.value, self._climb_bindings)
        if levels is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._climb_bindings[target.id] = levels

    def visit_Call(self, node: ast.Call) -> None:
        self._check_construction(node)
        # Tooth 3 too: an `os.path.dirname(...)` climb is a Call at its
        # outermost node, never an Attribute or a Subscript, so without this
        # the whole stdlib climb shape goes unrecorded.
        self._record_climb(node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._record_climb(node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._record_climb(node)
        self.generic_visit(node)

    def _record_climb(self, node: ast.AST) -> None:
        """Tooth 3 evidence gathering. Deliberately NOT a violation append:
        an escape is only decided once the whole module is walked, in
        `escape_violations`, because the outermost node of a climb chain is
        not known until its inner nodes have been visited."""
        if self._file_depth is None:
            return
        levels = _climb_levels_above_file(node, self._climb_bindings)
        if levels is None:
            return
        key = (node.lineno, self._symbol)
        if levels > self._climbs.get(key, -1):
            self._climbs[key] = levels

    def escape_violations(self) -> list[tuple[int, str, str, str]]:
        """Tooth 3: every climb that lands strictly above the repo root."""
        if self._file_depth is None:
            return []
        out = []
        for (lineno, symbol), levels in sorted(self._climbs.items()):
            if levels > self._file_depth:
                out.append((
                    lineno,
                    "above-repo-root-traversal",
                    symbol,
                    "resolves at the filesystem root"
                    if levels >= _CLIMB_TO_FILESYSTEM_ROOT
                    else f"climbs {levels} level(s) from a file {self._file_depth} below the repo root",
                ))
        return out

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div):
            # Only handle the outermost node of a `/`-chain: an inner Div
            # whose parent is also a Div would otherwise be double-flagged.
            self._check_construction(node)
        self.generic_visit(node)

    def _is_path_construction(self, node: ast.AST) -> bool:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return True
        if isinstance(node, ast.Call):
            func_name = _resolved_func_name(node.func)
            if func_name in ("Path", "join", "joinpath"):
                return True
        return False

    def _check_construction(self, node: ast.AST) -> None:
        if not self._is_path_construction(node):
            return

        # Tooth 1: root/drive-anchored literal as a constructor arg. `Path(...)`
        # only inspects args[0] (its own constructor semantics); `os.path.join`/
        # `joinpath` inspect every positional arg (any absolute-path arg resets
        # the join, regardless of position).
        if isinstance(node, ast.Call):
            func_name = _resolved_func_name(node.func)
            literal = _root_anchored_literal_arg(node, all_positions=func_name in ("join", "joinpath"))
            if literal is not None:
                self.violations.append(
                    (node.lineno, "root-or-drive-anchored-literal", self._symbol, literal.value)
                )

        # Tooth 2: __file__-anchored climb + sibling-repo token co-occurring
        # in the same construction (directly, or via a tainted Name ref).
        climbs = _is_climb_tainted_expr(node) or bool(self._tainted_names & _names_referenced(node))
        if climbs:
            tokens = _matched_sibling_tokens(_string_constants(node))
            if tokens:
                self.violations.append(
                    (node.lineno, "sibling-repo-crossing-traversal", self._symbol, ",".join(sorted(tokens)))
                )


def find_hardcoded_path_violations(root: Path, *, repo_root: Path | None = None) -> list[tuple[str, int, str, str]]:
    """Walk `root` and return every (relpath, lineno, tooth, detail) tuple
    for an unexempted violation.

    Per-tooth scope (amended 2026-07-25): Tooth 2
    (sibling-repo-crossing-traversal) is checked in BOTH production and
    `test_*.py` files — a misresolved sibling path in a test degrades it to
    silently-asserting-nothing rather than a visible failure, the exact
    defect class this amendment targets (see module docstring). Tooth 1
    (root-or-drive-anchored-literal) stays PRODUCTION-CODE-ONLY — in test
    code a root-anchored literal is almost always a mock argument handed to
    the function under test, not a portability hazard, and the 2026-07-25
    fallout measurement found 45 such sites, none of them a real
    cross-machine defect (see the two individually-adjudicated exceptions
    fixed directly: `test_sentinel.py`'s hardcoded-username literal, and
    `test_settings_home.py`'s `normalize_native_path` fixture confirmed as a
    genuine fixture, not a sibling-resolution site).

    `repo_root` defaults to `_REPO_ROOT`; a caller may override it (as the
    plant-a-violation tests do) so relpaths and exemption-set matching stay
    meaningful against an isolated tmp_path fixture tree.
    """
    repo_root = repo_root if repo_root is not None else _REPO_ROOT
    violations: list[tuple[str, int, str, str]] = []
    for path in sorted(root.rglob("*.py")):
        # Excluded files are still PARSED, but only Tooth 3 may speak for
        # them -- teeth 1 and 2 keep the narrower scope they were measured
        # against. Filtered below, after the visitor runs.
        tooth3_only = _is_excluded_source_path(path)
        is_test = _is_test_file(path)
        relpath = _relpath(path, repo_root)
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        try:
            depth = len(path.resolve().relative_to(repo_root.resolve()).parts)
        except ValueError:
            depth = None
        visitor = _HardcodedPathVisitor(file_depth=depth)
        visitor.visit(tree)
        for lineno, tooth, symbol, detail in visitor.violations + visitor.escape_violations():
            if tooth3_only and tooth != "above-repo-root-traversal":
                continue
            if is_test and tooth == "root-or-drive-anchored-literal":
                continue
            site = f"{relpath}::{symbol}"
            if site in _EXEMPT_SITES:
                continue
            violations.append((relpath, lineno, tooth, detail))
    return violations


def test_no_hardcoded_cross_repo_paths_in_production_code():
    """Standing gate: coordinator_core/ non-test code must contain zero
    unexempted hardcoded absolute/drive-anchored path literals or
    __file__-anchored sibling-repo-crossing directory traversals."""
    violations = find_hardcoded_path_violations(_SCAN_ROOT)
    assert violations == [], (
        "Found hardcoded-path violation(s) in coordinator_core/ production "
        "code (resolve via the machine-local registry / doe_root_pointer / "
        "trusted_root_guard instead, or add a named, dated _EXEMPT_SITES "
        f"entry per DEC-4): {violations}"
    )


def test_gate_detects_a_planted_parents_sibling_shellout(tmp_path):
    """Proves Tooth 2 catches the EXACT shape schema_drift_watch.py used to
    carry: a single-expression `Path(__file__).resolve().parents[N] /
    "<sibling>"` traversal."""
    fixture = tmp_path / "fixture_parents_sibling.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "\n"
        "def resolve_sibling():\n"
        "    return Path(__file__).resolve().parents[2].parent / \"DoE-claude\"\n",
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    matches = [v for v in violations if v[2] == "sibling-repo-crossing-traversal"]
    assert len(matches) == 1
    relpath, lineno, tooth, detail = matches[0]
    assert relpath.endswith("fixture_parents_sibling.py")
    assert lineno == 4
    assert detail == "DoE-claude"


def test_gate_detects_a_planted_split_statement_sibling_shellout(tmp_path):
    """Proves Tooth 2's same-module taint tracking catches the two-statement
    split shape schema_drift_watch.py's real bug actually had: the climb and
    the sibling-token literal in separate statements, joined by a variable."""
    fixture = tmp_path / "fixture_split_taint.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "\n"
        "def resolve_sibling():\n"
        "    claude_klabauter_root = Path(__file__).resolve().parents[2]\n"
        "    return claude_klabauter_root.parent / \"DoE-claude\"\n",
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    matches = [v for v in violations if v[2] == "sibling-repo-crossing-traversal"]
    assert len(matches) == 1
    relpath, lineno, tooth, detail = matches[0]
    assert relpath.endswith("fixture_split_taint.py")
    assert lineno == 5
    assert detail == "DoE-claude"


def test_gate_detects_a_planted_dirname_join_sibling_shellout(tmp_path):
    """Proves Tooth 2 catches the `os.path.dirname(...)` x N + ".." +
    "<sibling>" variant named explicitly in the dispatch brief, not just the
    pathlib `.parents[n]` shape."""
    fixture = tmp_path / "fixture_dirname_join.py"
    fixture.write_text(
        "import os\n"
        "\n"
        "def resolve_sibling():\n"
        "    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        "    return os.path.join(here, \"..\", \"example-retrieval-repo\")\n",
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    matches = [v for v in violations if v[2] == "sibling-repo-crossing-traversal"]
    assert len(matches) == 1
    relpath, lineno, tooth, detail = matches[0]
    assert relpath.endswith("fixture_dirname_join.py")
    assert lineno == 5
    assert detail == "example-retrieval-repo"


def test_gate_detects_a_planted_single_expression_dirname_join_str_segments(tmp_path):
    """Proves Tooth 2 catches the EXACT single-expression shape
    test_step_zero_emit.py's `_FIXTURE_CANDIDATES` carried: one
    `os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "..", "DoE-claude", ...)` call, string-segment `".."` climb style rather
    than pathlib `.parents[n]`, with the climb marker and the sibling token
    co-occurring directly in the same construction (no intermediate
    variable needed)."""
    fixture = tmp_path / "fixture_dirname_join_str_segments.py"
    fixture.write_text(
        "import os\n"
        "\n"
        "def resolve_fixture():\n"
        "    return os.path.join(\n"
        "        os.path.dirname(os.path.abspath(__file__)),\n"
        '        "..", "..", "..", "DoE-claude",\n'
        '        "coordinator", "tests", "fixtures", "step-zero-conformance.json",\n'
        "    )\n",
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    matches = [v for v in violations if v[2] == "sibling-repo-crossing-traversal"]
    assert len(matches) == 1
    relpath, lineno, tooth, detail = matches[0]
    assert relpath.endswith("fixture_dirname_join_str_segments.py")
    assert detail == "DoE-claude"


def test_gate_detects_a_planted_multi_hop_taint_sibling_shellout(tmp_path):
    """Proves Tooth 2 catches the EXACT multi-hop shape
    test_normalize_snippet.py's `_find_doe_normalize_lib` carried: the
    __file__-climb lands on `here`, a SECOND variable (`claude_klabauter_root`) is
    derived from `here` with no climb marker of its own re-appearing in
    that second assignment, and only a THIRD expression combines
    `claude_klabauter_root` with the sibling-repo token. Same-module same-pass taint
    tracking must propagate through the untainted-looking intermediate
    hop, not just a single assign-then-use step."""
    fixture = tmp_path / "fixture_multi_hop_taint.py"
    fixture.write_text(
        "import os\n"
        "\n"
        "def find_lib():\n"
        "    here = os.path.dirname(os.path.abspath(__file__))\n"
        '    claude_klabauter_root = os.path.abspath(os.path.join(here, "..", ".."))\n'
        '    return os.path.join(os.path.dirname(claude_klabauter_root), "DoE-claude")\n',
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    matches = [v for v in violations if v[2] == "sibling-repo-crossing-traversal"]
    assert len(matches) == 1
    relpath, lineno, tooth, detail = matches[0]
    assert relpath.endswith("fixture_multi_hop_taint.py")
    assert lineno == 6
    assert detail == "DoE-claude"


def test_gate_detects_a_planted_root_anchored_literal(tmp_path):
    """Proves Tooth 1 catches a hardcoded absolute-path literal even with no
    sibling-repo token and no __file__ climb involved at all."""
    fixture = tmp_path / "fixture_root_literal.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "\n"
        "def hardcoded_home():\n"
        "    return Path(\"/Users/someone/claude-klabauter\")\n",
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    matches = [v for v in violations if v[2] == "root-or-drive-anchored-literal"]
    assert len(matches) == 1
    relpath, lineno, tooth, detail = matches[0]
    assert relpath.endswith("fixture_root_literal.py")
    assert detail == "/Users/someone/claude-klabauter"


def test_gate_detects_a_planted_windows_drive_literal(tmp_path):
    """Proves Tooth 1 also catches a Windows drive-anchored literal, not
    only POSIX-rooted ones."""
    fixture = tmp_path / "fixture_drive_literal.py"
    fixture.write_text(
        "import os\n"
        "\n"
        "def hardcoded_home():\n"
        "    return os.path.join(\"C:\\\\DoE-claude\", \"coordinator\")\n",
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    matches = [v for v in violations if v[2] == "root-or-drive-anchored-literal"]
    assert len(matches) == 1


def test_gate_ignores_in_repo_only_climbing_and_doc_comment_mentions(tmp_path):
    """Negative control: a bare `parents[n]` climb with no sibling-repo
    token anywhere in the construction is benign (in-repo climbing is
    fine); a docstring/comment mentioning the hazardous shape in prose must
    not trip the AST-based scanner; a root-anchored `/tmp`-style literal
    with no sibling token is Tooth-1-eligible ONLY, not Tooth 2."""
    # Nested to a realistic depth: `parents[2]` from a file three components
    # below the root lands ON the root, which is the benign in-repo climb this
    # control is asserting about. At tmp_path's top level the same expression
    # would resolve two levels ABOVE the fixture root and Tooth 3 would be
    # right to flag it -- the fixture's own placement, not the code it plants.
    fixture = tmp_path / "pkg" / "sub" / "fixture_benign.py"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(
        '"""Uses Path(__file__).resolve().parents[4] / "DoE-claude" in prose only."""\n'
        "from pathlib import Path\n"
        "\n"
        "# A comment mentioning DoE-claude and parents[2] together is not code.\n"
        "def repo_root():\n"
        "    return Path(__file__).resolve().parents[2]\n"
        "\n"
        "def schemas_subpath():\n"
        "    return Path(\"coordinator\") / \"schemas\"\n",
        encoding="utf-8",
    )

    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)

    assert violations == []


def test_exempt_sites_do_not_trip_the_gate_by_construction():
    """The seed exemption set is meaningful (not vacuous): re-parse the two
    named exempt files directly and confirm their exempted symbol is the one
    actually carrying the flagged construct, so a future refactor that moves
    the construct to a DIFFERENT symbol in the same file re-trips the gate
    rather than silently riding the stale exemption."""
    pyresolve = _REPO_ROOT / "coordinator_core" / "pyresolve.py"
    lifecycle = _REPO_ROOT / "coordinator_core" / "lifecycle.py"
    assert pyresolve.is_file()
    assert lifecycle.is_file()

    unexempted = find_hardcoded_path_violations(_SCAN_ROOT)
    assert not any("pyresolve.py" in v[0] for v in unexempted)
    assert not any("lifecycle.py" in v[0] for v in unexempted)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def _plant(tmp_path: Path, relpath: str, body: str) -> Path:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("from pathlib import Path\n" + body + "\n", encoding="utf-8")
    return target


def test_tooth3_detects_a_planted_scratch_dir_above_the_repo_root(tmp_path):
    """The live violation this tooth was added for, in its original form.

    `bash_guards/tests/guard_message_corpus.py` climbed to the DRIVE ROOT and
    `mkdir`'d a scratch parent there, on every suite run, on whatever drive
    the checkout lived on. Teeth 1 and 2 were both blind to it: Tooth 1 wants
    a root-anchored string literal (there is none -- the path is computed),
    and Tooth 2 wants a known sibling-repo token as the leaf (the leaf is a
    scratch directory nobody had named before).
    """
    _plant(
        tmp_path,
        "coordinator_core/bash_guards/tests/guard_message_corpus.py",
        '_NEUTRAL_SCRATCH_PARENT = Path(__file__).resolve().parents[3].parent / "corpus-scratch"',
    )
    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)
    assert [(v[0], v[2]) for v in violations] == [
        ("coordinator_core/bash_guards/tests/guard_message_corpus.py", "above-repo-root-traversal")
    ]


def test_tooth3_detects_a_bare_parent_climb_with_no_leaf_at_all(tmp_path):
    """No `/` join, no string literal anywhere -- just a climb that lands
    above the root. Tooth 3 keys on WHERE the expression resolves, never on
    what is appended to it."""
    _plant(tmp_path, "coordinator_core/pkg/mod.py", "_X = Path(__file__).resolve().parents[2].parent")
    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)
    assert [v[2] for v in violations] == ["above-repo-root-traversal"]


def test_tooth3_detects_a_parents_index_one_past_the_repo_root(tmp_path):
    """The off-by-one shape: `parents[N]` where N is one deeper than the
    climb to the repo root. This is what `test_whoami_pin_migration.py` did
    -- binding the result to a variable named `repo_root` that actually held
    the drive root, which silently degraded its `!=` assertion to always
    true."""
    _plant(tmp_path, "coordinator_core/a/b/mod.py", "_ROOT = str(Path(__file__).resolve().parents[4])")
    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)
    assert [v[2] for v in violations] == ["above-repo-root-traversal"]


def test_tooth3_allows_a_climb_that_lands_exactly_on_the_repo_root(tmp_path):
    """The negative direction, and the reason this tooth counts rather than
    pattern-matches: reaching the repo root is the single most common thing
    these climbs do. `parents[3]` from a file three directories deep is the
    root itself, not an escape."""
    _plant(tmp_path, "coordinator_core/a/b/mod.py", "_ROOT = Path(__file__).resolve().parents[3]")
    assert find_hardcoded_path_violations(tmp_path, repo_root=tmp_path) == []


def test_tooth3_ignores_a_climb_anchored_at_another_modules_file(tmp_path):
    """`<module>.__file__` starts somewhere this gate cannot measure -- the
    module may have been loaded from an entirely different checkout, which is
    exactly what `test_engine_root_conformance.py` does. Counting its levels
    against the SCANNED file's depth would report an escape that is not one,
    so an unmeasurable anchor yields no verdict."""
    _plant(tmp_path, "coordinator_core/a/b/mod.py", "_X = Path(mr.__file__).resolve().parents[3].parent")
    assert find_hardcoded_path_violations(tmp_path, repo_root=tmp_path) == []


def test_tooth3_sees_helper_modules_under_a_tests_directory(tmp_path):
    """Scope proof. Teeth 1 and 2 skip a non-`test_*.py` file under `tests/`
    outright, which is precisely the slot the real violation occupied for
    months. Tooth 3 must not inherit that blind spot."""
    _plant(tmp_path, "coordinator_core/pkg/tests/helper_corpus.py", "_X = Path(__file__).resolve().parents[3].parent")
    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)
    assert [v[2] for v in violations] == ["above-repo-root-traversal"]


def test_no_above_repo_root_traversal_anywhere_in_coordinator_core():
    """Standing gate: nothing under `coordinator_core/` may resolve a path
    above this repo's own root. Every repo has its own scratch folder; the
    wider drive is not ours to write into (PM ruling, 2026-08-28)."""
    violations = [
        v for v in find_hardcoded_path_violations(_SCAN_ROOT) if v[2] == "above-repo-root-traversal"
    ]
    assert violations == [], (
        "path expression(s) resolving above the repo root: %r" % (violations,)
    )


def test_tooth3_traces_a_climb_through_an_intermediate_variable(tmp_path):
    """Aliasing the anchor to a local is an ordinary refactor, not an exotic
    evasion -- the tooth must follow it across statements."""
    _plant(
        tmp_path,
        "coordinator_core/a/b/mod.py",
        "_P = Path(__file__).resolve()\n_Q = _P.parents[3].parent",
    )
    assert [v[2] for v in find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)] == [
        "above-repo-root-traversal"
    ]


def test_tooth3_traces_an_aliased_dunder_file(tmp_path):
    """`_FILE = __file__` defeats a bare-Name anchor check unless the binding
    is traced too."""
    _plant(tmp_path, "coordinator_core/a/b/mod.py", "_FILE = __file__\n_R = Path(_FILE).parents[4]")
    assert [v[2] for v in find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)] == [
        "above-repo-root-traversal"
    ]


def test_tooth3_detects_a_negative_parents_index(tmp_path):
    """`parents[-1]` is the filesystem root -- the most extreme climb there
    is, and the one shape a plain `ast.Constant` check cannot see because it
    parses as `UnaryOp(USub, Constant)`."""
    _plant(tmp_path, "coordinator_core/a/b/mod.py", "_R = Path(__file__).resolve().parents[-1]")
    violations = find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)
    assert [(v[2], v[3]) for v in violations] == [
        ("above-repo-root-traversal", "resolves at the filesystem root")
    ]


def test_tooth3_detects_dotdot_segments_and_dirname_chains(tmp_path):
    """Climbing by `".."` string or by nested `os.path.dirname` counts the
    same as climbing by `.parent`."""
    # `.parent` is one level, so FOUR `".."` segments are needed to clear a
    # file four components below the root -- three would land exactly on it.
    _plant(tmp_path, "coordinator_core/a/b/mod.py", '_R = Path(__file__).parent.joinpath("..", "..", "..", "..")')
    assert [v[2] for v in find_hardcoded_path_violations(tmp_path, repo_root=tmp_path)] == [
        "above-repo-root-traversal"
    ]

    other = tmp_path / "other"
    # FIVE dirname wraps, not four: from a file four components below the
    # root, four of them lands ON the root and is not an escape. The
    # off-by-one is the whole point of the tooth, so the fixture has to clear
    # it rather than sit on it.
    _plant(
        other,
        "coordinator_core/a/b/mod.py",
        "import os\n_R = os.path.dirname(os.path.dirname(os.path.dirname("
        "os.path.dirname(os.path.dirname(__file__)))))",
    )
    assert [v[2] for v in find_hardcoded_path_violations(other, repo_root=other)] == [
        "above-repo-root-traversal"
    ]


def test_tooth3_subtracts_a_descent_back_into_the_repo(tmp_path):
    """The false-positive direction, and the reason segments are counted
    rather than ignored: reaching the repo root and walking back down into it
    is the most common shape in this repo and is not an escape."""
    _plant(
        tmp_path,
        "coordinator_core/a/b/mod.py",
        '_R = Path(__file__).resolve().parents[3] / "coordinator" / "bin"',
    )
    assert find_hardcoded_path_violations(tmp_path, repo_root=tmp_path) == []


def test_tooth3_declines_to_guess_at_a_non_literal_segment(tmp_path):
    """An unresolvable segment yields no verdict, never a guess -- the same
    posture as a non-literal `parents[i]` subscript."""
    _plant(
        tmp_path,
        "coordinator_core/a/b/mod.py",
        "def f(name):\n    return Path(__file__).resolve().parents[3] / name",
    )
    assert find_hardcoded_path_violations(tmp_path, repo_root=tmp_path) == []
