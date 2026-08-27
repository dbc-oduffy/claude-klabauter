"""Unit tests for `coordinator_core.win_portability.leaf_spawn_creationflags`,
plus the standing gate that keeps it honest.

Context: `docs/plans/2026-08-25-leaf-spawns-stop-paying-for-a-console.md` C3
adds a second, STRONGER console-suppression primitive alongside
`no_console_creationflags()` -- `DETACHED_PROCESS` rather than
`CREATE_NO_WINDOW` -- for a caller that knows its child is a genuine leaf
spawn with no console-subsystem descendants of its own. `DETACHED_PROCESS`
leaves the child with NO console at all (stronger than a windowless one),
which is exactly wrong for a child that itself spawns further
console-subsystem processes: each such grandchild allocates its own fresh,
VISIBLE console, because there is no windowless console for it to inherit.
See `leaf_spawn_creationflags()`'s own docstring for the full hazard.

This module is also the GATE (per this chunk's own body: "a second helper
... and the gate that keeps it honest") -- three axes, all standing/fast-tier:

  (a) `DETACHED_PROCESS | CREATE_NO_WINDOW` (in any spelling -- OR'd flags,
      two `creationflags=` compositions, a dict literal carrying both keys)
      never appears anywhere in the repo. Win32 documents `CREATE_NO_WINDOW`
      as IGNORED whenever `DETACHED_PROCESS` (or `CREATE_NEW_CONSOLE`) is
      also set, so this exact combination reads as console-suppressed while
      behaving as bare `DETACHED_PROCESS` -- the precise defect
      `auto_push._windows_detached_flags` was created to fix (measured: 6
      visible `conhost.exe` windows across 3 spawns before the fix; see that
      function's own docstring).

  (b) every `leaf_spawn_creationflags()` call site passes a stdio kwarg
      (`stdin=`/`stdout=`/`stderr=`/`capture_output=`, or a `**splat` that
      resolves to one). Under `DETACHED_PROCESS` there is no console for an
      unwired child to inherit standard handles from at all -- unlike
      `no_console_creationflags()`'s windowless-but-present console, the
      handles here are INVALID, not merely unread, so a call site with none
      of the four is broken by construction, not just lossy.

  (c) the two named exclusions -- `spawn-hidden.py :: _NO_WINDOW` and
      `auto_push.py :: _windows_detached_flags` -- never call
      `leaf_spawn_creationflags()`. Both already carry their own, deliberately
      different, hand-tuned creation-flag composition (see each site's own
      docstring); migrating either onto this shared leaf helper is exactly the
      regression axis (a) exists to catch, so this axis catches it one step
      earlier, at the call site itself, before a bad composition could even
      be written.

Enumeration reuses `coordinator_core.spawn_policy` traversal
(`discover_source_files`, `DEFAULT_EXCLUDE`, `is_test_tree_site`) -- the same
primitives `test_no_bare_hot_path_spawn.py` walks the repo with -- rather than
re-deriving a second walk. That gate is value-blind to this one (it asserts
`creationflags=` presence generically; it does not know `DETACHED_PROCESS`
from `CREATE_NO_WINDOW`) and stays green throughout this chunk -- nothing
here changes its behavior or its assertions.

Spec backlink: state/dispatch-briefs/2026-08-25-leaf-spawns-stop-paying-for-a-console/C3.md
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from coordinator_core.spawn_policy import SpawnParseError, is_test_tree_site
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files
from coordinator_core.win_portability import leaf_spawn_creationflags

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Scope for axes (a)/(c): the two live planes this primitive and its named
#: exclusions actually live in (`coordinator_core`, `coordinator`) --
#: deliberately NOT the whole repo. `state/roadmap/**/spikes/` holds
#: intentionally-exploratory, never-shipped scratch scripts (e.g.
#: `pcore-01-hook-shim.py`) that predate this workstream and are out of its
#: writes-scope to touch; scoping the gate to the shipped planes keeps it
#: standing/fast-tier-safe without silently blessing that spike's own
#: combination via an ad-hoc per-file exemption.
_GATE_SCOPE_ROOTS: tuple[str, ...] = ("coordinator_core", "coordinator")

_STDIO_KWARGS = frozenset({"stdin", "stdout", "stderr", "capture_output"})

#: The two sites this chunk's brief names as never permitted to migrate onto
#: `leaf_spawn_creationflags()` -- see module docstring axis (c).
_NAMED_EXCLUSION_SITES: tuple[tuple[str, str], ...] = (
    ("coordinator/lib/spawn-hidden.py", "_NO_WINDOW"),
    ("coordinator_core/hooks/auto_push.py", "_windows_detached_flags"),
)


def test_leaf_spawn_creationflags_posix_is_empty(monkeypatch):
    monkeypatch.setattr("coordinator_core.win_portability._is_windows", lambda: False)
    assert leaf_spawn_creationflags() == {}


def test_leaf_spawn_creationflags_windows_uses_detached_process(monkeypatch):
    monkeypatch.setattr("coordinator_core.win_portability._is_windows", lambda: True)
    import subprocess as real_subprocess

    monkeypatch.setattr(real_subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)

    result = leaf_spawn_creationflags()

    assert result == {"creationflags": 0x00000008}


def test_leaf_spawn_creationflags_windows_falls_back_when_constant_absent(monkeypatch):
    """Mirrors `no_console_creationflags()`'s own monkeypatch-seam self-test:
    `_is_windows()` patched True on a real POSIX host must not raise
    `AttributeError` reaching for the Windows-only `DETACHED_PROCESS`
    constant -- the `getattr(..., 0)` fallback is what this proves."""
    monkeypatch.setattr("coordinator_core.win_portability._is_windows", lambda: True)
    import subprocess as real_subprocess

    monkeypatch.delattr(real_subprocess, "DETACHED_PROCESS", raising=False)

    result = leaf_spawn_creationflags()

    assert result == {"creationflags": 0}


def test_leaf_spawn_creationflags_docstring_carries_subtree_hazard_and_named_sites():
    doc = leaf_spawn_creationflags.__doc__ or ""
    assert "no console" in doc.lower()
    assert "spawn-hidden.py" in doc and "_NO_WINDOW" in doc
    assert "auto_push.py" in doc and "_windows_detached_flags" in doc


# --------------------------------------------------------------------------
# Gate axis (a): DETACHED_PROCESS | CREATE_NO_WINDOW never appears together.
# --------------------------------------------------------------------------


def _names_in_expr(node: ast.expr) -> set[str]:
    """Every bare identifier reachable inside `node`, e.g. both
    `subprocess.DETACHED_PROCESS` and a bare `DETACHED_PROCESS` (from a
    `from subprocess import DETACHED_PROCESS`-shaped import) resolve to the
    name `DETACHED_PROCESS`. Deliberately identifier-shaped, not
    module-resolved -- consistent with this repo's existing AST gates'
    false-negative-over-false-positive stance (see
    `test_no_bare_hot_path_spawn.py`'s own docstring)."""

    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            names.add(sub.value)
    return names


def _both_flags_present(node: ast.expr) -> bool:
    names = _names_in_expr(node)
    return "DETACHED_PROCESS" in names and "CREATE_NO_WINDOW" in names


class _BothFlagsVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.linenos: list[int] = []

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.BitOr) and _both_flags_present(node):
            self.linenos.append(node.lineno)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        if _both_flags_present(node):
            self.linenos.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # `flags |= X; flags |= Y` compositions (auto_push's own pre-fix
        # shape) don't form a single BinOp -- caught instead by scanning
        # the enclosing function body for both augmented-assign targets.
        self.generic_visit(node)


def _augassign_both_flags(body: list[ast.stmt]) -> list[int]:
    """Detects the `flags |= DETACHED_PROCESS` / `flags |= CREATE_NO_WINDOW`
    two-statement composition shape (rather than a single `BinOp`) within
    one function body -- same variable, both flags, anywhere in the same
    scope, not nested inside a further function."""

    hits: list[int] = []
    target_flags: dict[str, set[str]] = {}
    target_linenos: dict[str, int] = {}

    def _walk(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if (
                isinstance(stmt, ast.AugAssign)
                and isinstance(stmt.op, ast.BitOr)
                and isinstance(stmt.target, ast.Name)
            ):
                names = _names_in_expr(stmt.value)
                relevant = names & {"DETACHED_PROCESS", "CREATE_NO_WINDOW"}
                if relevant:
                    seen = target_flags.setdefault(stmt.target.id, set())
                    seen |= relevant
                    target_linenos.setdefault(stmt.target.id, stmt.lineno)
            for field in ("body", "orelse", "finalbody"):
                child = getattr(stmt, field, None)
                if child:
                    _walk(child)
            handlers = getattr(stmt, "handlers", None)
            if handlers:
                for handler in handlers:
                    _walk(handler.body)

    _walk(body)
    for target, flags in target_flags.items():
        if {"DETACHED_PROCESS", "CREATE_NO_WINDOW"} <= flags:
            hits.append(target_linenos[target])
    return hits


def _find_both_flags_violations(
    root: pathlib.Path, scope_roots: tuple[str, ...] | None = None
) -> list[tuple[str, int]]:
    discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
    violations: list[tuple[str, int]] = []

    for relpath, file_path in discovered:
        if scope_roots is not None and not relpath.split("/", 1)[0] in scope_roots:
            continue
        if is_test_tree_site(relpath):
            continue
        source = file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:
            raise SpawnParseError(relpath, str(exc)) from exc

        visitor = _BothFlagsVisitor()
        visitor.visit(tree)
        for lineno in visitor.linenos:
            violations.append((relpath, lineno))

        for func_body in (
            node.body for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for lineno in _augassign_both_flags(func_body):
                violations.append((relpath, lineno))

    return violations


def test_no_detached_process_and_no_window_combined():
    """Gate axis (a) -- see module docstring. Standing/fast-tier: must stay
    green now that `auto_push._windows_detached_flags` no longer composes
    both flags together (fixed 2026-08-21, see that function's own
    docstring measurement)."""
    violations = _find_both_flags_violations(REPO_ROOT, scope_roots=_GATE_SCOPE_ROOTS)
    assert violations == [], "\n".join(f"{path}:{lineno}" for path, lineno in violations)


def test_gate_axis_a_detects_a_planted_combined_flags_binop(tmp_path):
    fixture = tmp_path / "planted_binop.py"
    fixture.write_text(
        "import subprocess\n"
        "flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW\n",
        encoding="utf-8",
    )
    violations = _find_both_flags_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0][0].endswith("planted_binop.py")


def test_gate_axis_a_detects_a_planted_combined_flags_augassign(tmp_path):
    fixture = tmp_path / "planted_augassign.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def compose():\n"
        "    flags = 0\n"
        "    flags |= subprocess.DETACHED_PROCESS\n"
        "    flags |= subprocess.CREATE_NO_WINDOW\n"
        "    return flags\n",
        encoding="utf-8",
    )
    violations = _find_both_flags_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0][0].endswith("planted_augassign.py")


def test_gate_axis_a_ignores_a_single_flag(tmp_path):
    fixture = tmp_path / "clean.py"
    fixture.write_text(
        "import subprocess\n"
        "flags = subprocess.DETACHED_PROCESS\n",
        encoding="utf-8",
    )
    violations = _find_both_flags_violations(tmp_path)
    assert violations == []


# --------------------------------------------------------------------------
# Gate axis (b): every leaf_spawn_creationflags() call site wires stdio.
# --------------------------------------------------------------------------


def _call_func_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_leaf_spawn_call(node: ast.expr) -> bool:
    name = _call_func_name(node)
    return name == "leaf_spawn_creationflags"


class _LeafSpawnUseVisitor(ast.NodeVisitor):
    """Finds every `subprocess.run(...)`/`Popen(...)`-shaped call whose
    `creationflags=` keyword (directly, or via a `**splat` that traces back
    to a `NAME = leaf_spawn_creationflags()`-shaped assignment in the same
    or an enclosing scope) resolves to `leaf_spawn_creationflags()`, and
    records whether that call also wires a stdio kwarg. Deliberately does
    NOT require the call to resolve to `subprocess` specifically -- any
    call shape splatting or keying off this identifier is in scope, matching
    axis (b)'s "every call site" wording."""

    def __init__(self) -> None:
        self.sites: list[tuple[int, bool]] = []  # (lineno, has_stdio)
        self._no_console_stack: list[set[str]] = [set()]

    def _current_names(self) -> set[str]:
        out: set[str] = set()
        for scope in self._no_console_stack:
            out |= scope
        return out

    def _collect_local_names(self, body: list[ast.stmt]) -> set[str]:
        names: set[str] = set()

        def _walk(stmts: list[ast.stmt]) -> None:
            for stmt in stmts:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                targets = None
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets = [stmt.target]
                if targets is not None and isinstance(stmt.value, ast.Call) and _is_leaf_spawn_call(stmt.value.func):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
                for field in ("body", "orelse", "finalbody"):
                    child = getattr(stmt, field, None)
                    if child:
                        _walk(child)
                handlers = getattr(stmt, "handlers", None)
                if handlers:
                    for handler in handlers:
                        _walk(handler.body)

        _walk(body)
        return names

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._no_console_stack.append(self._collect_local_names(node.body))
        self.generic_visit(node)
        self._no_console_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        has_direct = False
        has_stdio = any(kw.arg in _STDIO_KWARGS for kw in node.keywords)
        for kw in node.keywords:
            if kw.arg == "creationflags" and isinstance(kw.value, ast.Call) and _is_leaf_spawn_call(kw.value.func):
                has_direct = True
            if kw.arg is None:  # **splat
                target = kw.value
                if isinstance(target, ast.Call) and _is_leaf_spawn_call(target.func):
                    has_direct = True
                elif isinstance(target, ast.Name) and target.id in self._current_names():
                    has_direct = True
        if has_direct:
            self.sites.append((node.lineno, has_stdio))
        self.generic_visit(node)


def _find_leaf_spawn_uses(root: pathlib.Path) -> list[tuple[str, int, bool]]:
    discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
    uses: list[tuple[str, int, bool]] = []

    for relpath, file_path in discovered:
        if is_test_tree_site(relpath):
            continue
        source = file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:
            raise SpawnParseError(relpath, str(exc)) from exc

        visitor = _LeafSpawnUseVisitor()
        visitor.visit(tree)
        for lineno, has_stdio in visitor.sites:
            uses.append((relpath, lineno, has_stdio))

    return uses


def test_every_leaf_spawn_creationflags_site_wires_stdio():
    """Gate axis (b) -- see module docstring. Empty by construction: this
    chunk introduces the helper but migrates no call site onto it (that is
    a later wave's job), so there are zero sites to check yet."""
    uses = _find_leaf_spawn_uses(REPO_ROOT / "coordinator_core")
    unwired = [(path, lineno) for path, lineno, has_stdio in uses if not has_stdio]
    assert unwired == [], "\n".join(f"{path}:{lineno} -- no stdio kwarg" for path, lineno in unwired)


def test_gate_axis_b_detects_a_planted_unwired_leaf_spawn(tmp_path):
    fixture = tmp_path / "planted_unwired.py"
    fixture.write_text(
        "import subprocess\n"
        "from coordinator_core.win_portability import leaf_spawn_creationflags\n"
        "\n"
        "def spawn():\n"
        "    subprocess.Popen(['git', 'gc'], **leaf_spawn_creationflags())\n",
        encoding="utf-8",
    )
    uses = _find_leaf_spawn_uses(tmp_path)
    assert len(uses) == 1
    _path, _lineno, has_stdio = uses[0]
    assert has_stdio is False


def test_gate_axis_b_ignores_a_wired_leaf_spawn(tmp_path):
    fixture = tmp_path / "planted_wired.py"
    fixture.write_text(
        "import subprocess\n"
        "from coordinator_core.win_portability import leaf_spawn_creationflags\n"
        "\n"
        "def spawn():\n"
        "    subprocess.Popen(\n"
        "        ['git', 'gc'],\n"
        "        stdout=subprocess.DEVNULL,\n"
        "        **leaf_spawn_creationflags(),\n"
        "    )\n",
        encoding="utf-8",
    )
    uses = _find_leaf_spawn_uses(tmp_path)
    assert len(uses) == 1
    _path, _lineno, has_stdio = uses[0]
    assert has_stdio is True


# --------------------------------------------------------------------------
# Gate axis (c): the two named exclusions never call leaf_spawn_creationflags.
# --------------------------------------------------------------------------


def test_named_exclusion_sites_never_call_leaf_spawn_creationflags():
    """Gate axis (c) -- see module docstring. Checked two ways: (1) the
    named source files, if present on disk, must not reference the
    identifier `leaf_spawn_creationflags` anywhere at all (a stronger check
    than "not called" -- an import alone would be the first step toward a
    migration this chunk forbids); (2) the enumerated call sites from axis
    (b) never land inside either named function, as a second, independent
    cross-check using the same enclosing-function-name signal
    `test_no_bare_hot_path_spawn.py` itself records per site."""
    for relpath, function_name in _NAMED_EXCLUSION_SITES:
        file_path = REPO_ROOT / relpath
        if not file_path.exists():
            continue
        source = file_path.read_text(encoding="utf-8")
        assert "leaf_spawn_creationflags" not in source, (
            f"{relpath} references leaf_spawn_creationflags -- {function_name} "
            f"must never migrate onto the leaf-spawn primitive, see that "
            f"function's own docstring"
        )
