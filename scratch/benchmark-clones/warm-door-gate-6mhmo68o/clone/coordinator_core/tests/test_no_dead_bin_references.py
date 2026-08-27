"""
coordinator_core.tests.test_no_dead_bin_references -- static gate proving
every in-repo executable path referenced, by literal path construction,
from production code under coordinator_core/ actually EXISTS on disk at
HEAD.

Motivating incident (spec backlink): state/lessons/2026-08-19-a-guard-on-
a-deleted-binary-fails-silently-forever.md.
``coordinator_core/install/first_run.py::_seed_machine_local_registry``
guarded ``is_executable(machine_local_bin)`` where ``machine_local_bin`` was
``<claude_klabauter_root>/coordinator/bin/machine-local``. That file (and its
``.cmd``/``.ps1`` Windows twins) was deleted 2026-08-14 in ``3bd2738f4`` --
"delete the three dead bareword forwarders" -- a change that swept import
graph and call-site reachability and found none, because the only remaining
reference was a runtime PATH STRING inside a guard, not an import or a
call. From that date, the guard's ``is_executable`` check failed
permanently, so the warn-and-continue branch became unconditional: first-run
seeded the machine-local registry on ZERO fresh installs for five days.
Nothing fired -- not a test, not a doctor probe, not an install-health leg --
because every layer in isolation was behaving correctly (see the lesson for
the full "why nothing caught it" account). The lesson names the missing
artifact explicitly: "a guard asserting that every in-repo executable path
referenced by an install-chain capability check actually exists at HEAD."
This module is that artifact, generalised to any production reference of
this shape (not install-chain-only).

WHAT THIS GATE RESOLVES (the only shapes it claims to catch):
  1. A ``BinOp(Div)`` chain (``root / "a" / "b" / ...``) or a bare
     ``os.path.join(root, "a", "b", ...)`` call, where every segment after
     the root is a string-literal constant.
  2. ``root`` is a bare ``ast.Name`` whose identifier is one of this repo's
     own naming-convention tokens for "the claude-klabauter repo root":
     ``_REPO_ROOT``, ``_CLAUDE_KLABAUTER_ROOT``, ``claude_klabauter_root``, ``repo_root``
     (exact match; see ``_REPO_ROOT_CONVENTION_NAMES``). This is a stated,
     checked convention, not a guess: a `git grep` at authoring time found
     every module-level ``_REPO_ROOT``/``_CLAUDE_KLABAUTER_ROOT`` constant in this
     tree computed via a ``Path(__file__).resolve().parents[N]`` climb, and
     every ``claude_klabauter_root``/``repo_root`` parameter fed by a real repo-root
     resolver at its call sites -- this gate trusts that naming discipline
     rather than re-deriving each site's own climb arithmetic (see "WHAT
     THIS GATE DECLINES" below for why re-deriving it is out of scope).
  3. Among the literal segments, at least one must equal ``"bin"`` or
     ``"scripts"`` (the bin-ish directory names named in the dispatch
     brief), with at least one further literal segment after it -- that
     final segment is the referenced basename.
  4. Existence check for the basename, against ``resolve_root`` (the real
     repo root in production use) joined with every literal segment up to
     but not including the basename:
       - basename WITH an explicit extension (``.py``, ``.cmd``, ...):
         exact-file existence only.
       - basename with NO extension (coordinator's own bareword CLI
         entrypoints, e.g. ``machine-local``): exists if the bare file OR
         any of ``.py``/``.cmd``/``.ps1``/``.sh`` sibling exists. This
         mirrors ``win_portability.is_executable``'s own PATHEXT-sibling
         forgiveness (an extensionless reference IS launchable via a
         ``.cmd`` twin on Windows) and additionally accepts a lone ``.py``
         sibling as a legitimate forwarder-resolution target (per the
         dispatch brief: "a reference to foo where only foo.py exists may
         be legitimate") -- deliberately permissive here, because the
         asymmetric cost this gate is tuned for is a missed TOTAL deletion
         (the real incident: bare file AND both twins gone), not a
         disagreement over which single sibling is canonical.

WHAT THIS GATE DECLINES (by design, not oversight -- a guard that guesses
is worse than none per the dispatch brief):
  - Any root ``Name`` NOT in ``_REPO_ROOT_CONVENTION_NAMES`` -- e.g.
    ``plugin_root``, ``settings_home``, ``claude_home``, ``home``,
    ``bin_dst``, ``coord_root``. These denote OTHER roots this repo's git
    history does not control (an install destination, ``~/.claude``, a
    sibling checkout) -- flagging them would accuse a target this gate has
    no authority to judge absent/present.

    KNOWN UNCOVERED SITE, and why widening the set is the wrong fix
    (2026-08-19 review). ``ops/central_run_due.py`` spells a genuine
    repo-root climb ``engine_root`` and joins it to
    ``coordinator/bin/extract-lessons.py`` -- exactly this gate's shape,
    invisible to it, and its dead-binary protection rests entirely on its
    own runtime ``isfile`` guard. Adding ``engine_root`` to the convention
    set would NOT fix that, it would break the premise the set rests on:
    the same spelling denotes the UNREAL ENGINE root in
    ``install/prereq_probe.py :: _ue_check_dir``, a tree this repo does not
    control at all. That it does not currently collide is an accident of
    that function joining a loop variable (declined for non-literal
    segments), not a property anyone maintains. A name meaning two
    different roots cannot carry a naming discipline, so the honest remedy
    is at the call site -- rename it, or route it through the resolver --
    not here. Left uncovered deliberately, and named so the next reader
    finds it rather than rediscovers it.
  - Any chain/call containing a non-literal (variable) segment anywhere
    after the root -- a dynamically-built basename or intermediate segment
    cannot be resolved without executing the code, which this gate never
    does.
  - Cross-statement taint tracking (unlike the sibling gate
    ``test_no_hardcoded_paths.py``'s Tooth 2): only a SINGLE expression is
    flattened. ``x = repo_root / "coordinator"; y = x / "bin" / "name"``
    is not followed -- declined, same accepted-false-negative shape that
    gate's own docstring names for cross-module construction.
  - ``Path(__file__).resolve().parents[N]`` (or ``os.path.dirname`` chains)
    used directly as the root of the SAME expression, rather than pre-bound
    to one of the recognised convention names first. The convention-name
    rule above already covers the common case (every real production site
    found at authoring time binds the climb to ``_REPO_ROOT``/
    ``_CLAUDE_KLABAUTER_ROOT`` before joining "coordinator"/"bin"); requiring the
    bound name means this gate never has to re-verify a climb depth's own
    correctness, which is a materially different (and already differently
    tested) concern.
  - ``.joinpath(...)`` calls -- every production use found at authoring
    time either star-unpacks a variable tuple or passes a non-literal
    argument, so declining the shape entirely costs nothing here; adding
    support would need multi-level Attribute-skip parent tracking for no
    observed benefit.
  - Test files (``test_*.py``) and any non-test file under a ``tests/``
    directory -- production-code-only scope per the dispatch brief. A
    fixture naming a fake binary under ``tmp_path`` must never trip this
    gate, and excluding test files entirely is the simplest sound way to
    guarantee that (test-only real-path assertions are that file's own
    concern, not this gate's).

No exemption set: unlike ``test_no_hardcoded_paths.py``, this gate does not
carry a ``_EXEMPT_SITES`` register. A register of prose claims about why a
missing target is fine is exactly the artifact class the amplification
exemption-register burn-down (the audit that found the motivating incident
in the first place) is actively retiring -- see the lesson's "How it was
actually found" section. A genuine false positive here is adjudicated by
narrowing this gate's resolution rules (a code change to this file,
reviewed like any other), never by naming a site as exempt from a check the
gate itself cannot re-verify later.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

_REPO_ROOT_CONVENTION_NAMES = {"_REPO_ROOT", "_CLAUDE_KLABAUTER_ROOT", "claude_klabauter_root", "repo_root"}

_BINISH_SEGMENTS = {"bin", "scripts"}

# Basename existence-check siblings tried for an extensionless literal, in
# the order named in the module docstring's § point 4.
#
# A SUPERSET of `win_portability.is_executable`'s PATHEXT set, not a mirror of it -- the two
# answer different questions and the difference is deliberate in both directions. `.exe`/`.bat`
# are real PATHEXT members and MUST be here: a reference to `foo` satisfied only by `foo.exe`
# would otherwise be reported dead, a false positive in a gate whose findings are meant to be
# actionable without triage. `.py`/`.ps1`/`.sh` are NOT PATHEXT members and are here anyway,
# because this gate asks "does anything plausibly satisfy this reference" rather than "would
# CreateProcess launch this directly" -- the asymmetric cost it is tuned for is a missed TOTAL
# deletion (the real incident: bare file AND every twin gone), never a disagreement over which
# single sibling is canonical.
_EXTENSIONLESS_SIBLINGS = ("", ".py", ".cmd", ".exe", ".bat", ".ps1", ".sh")


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_")


def _is_excluded_source_path(path: Path) -> bool:
    """Production-code-only scope (see module docstring's last declined
    bullet): every file under a ``tests/`` directory is skipped, plus any
    ``test_*.py`` file anywhere (mirrors ``test_no_hardcoded_paths.py``'s
    own test-file predicate, but this gate excludes ALL test files -- it
    does not selectively widen into them the way that gate's Tooth 2
    does)."""
    return "tests" in path.parts or _is_test_file(path)


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _resolved_func_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _flatten(node: ast.AST) -> tuple[ast.AST, list[str]] | tuple[None, None]:
    """Reduce a path-construction expression to ``(root_node, [literal
    segments])``, or ``(None, None)`` if any part of it cannot be resolved
    statically (see module docstring's "WHAT THIS GATE DECLINES")."""
    if isinstance(node, ast.Name):
        return (node, [])

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left_root, left_segs = _flatten(node.left)
        if left_root is None:
            return (None, None)
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return (left_root, [*left_segs, node.right.value])
        return (None, None)

    if isinstance(node, ast.Call):
        func_name = _resolved_func_name(node.func)
        if func_name == "join" and node.args:
            base_root, base_segs = _flatten(node.args[0])
            if base_root is None:
                return (None, None)
            segs = list(base_segs)
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    segs.append(arg.value)
                else:
                    return (None, None)
            return (base_root, segs)
        if func_name == "Path" and node.args:
            return _flatten(node.args[0])
        return (None, None)

    return (None, None)


def _existing_candidate(base_dir: Path, basename: str) -> bool:
    """True if the reference resolves to something real on disk.

    A DIRECTORY at the exact bare name (e.g. ``coordinator/bin/lib``,
    ``coordinator/bin/install-health``) counts as existing outright, checked
    before the file-shaped rules below: this gate's basename segment is
    "whatever the final literal path segment names", and that is
    legitimately a subdirectory at several real call sites found at
    authoring time, not always a launchable file. Requiring file-ness there
    would have produced a false positive on every one of them -- the exact
    failure mode the dispatch brief warns a guessing guard produces."""
    if (base_dir / basename).is_dir():
        return True
    if Path(basename).suffix:
        return (base_dir / basename).is_file()
    return any((base_dir / f"{basename}{ext}").is_file() for ext in _EXTENSIONLESS_SIBLINGS)


class _DeadBinRefVisitor(ast.NodeVisitor):
    """Single forward pass over one module. Only inspects the OUTERMOST
    node of a Div-chain or ``os.path.join`` call (an inner node whose
    immediate parent is itself part of the same construction is skipped,
    via the parent stack, to avoid re-flattening and double-reporting the
    same reference)."""

    def __init__(self) -> None:
        # (lineno, symbol, target_relpath) -- every matched bin-ish
        # reference, NOT yet filtered by existence (that filter needs
        # `resolve_root`, which this visitor doesn't know about; see
        # `find_dead_bin_references`).
        self.candidates: list[tuple[int, str, str]] = []
        self._parents: list[ast.AST] = []
        self._fn_stack: list[str] = ["<module>"]

    def _parent(self) -> ast.AST | None:
        return self._parents[-2] if len(self._parents) >= 2 else None

    def visit(self, node: ast.AST) -> None:  # noqa: D102 -- parent-tracking wrapper
        self._parents.append(node)
        super().visit(node)
        self._parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._fn_stack.append(node.name)
        self.generic_visit(node)
        self._fn_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div):
            parent = self._parent()
            is_nested = isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div)
            if not is_nested:
                self._check(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _resolved_func_name(node.func) == "join":
            self._check(node)
        self.generic_visit(node)

    def _check(self, node: ast.AST) -> None:
        root, segments = _flatten(node)
        if root is None or not isinstance(root, ast.Name):
            return
        if root.id not in _REPO_ROOT_CONVENTION_NAMES:
            return
        if len(segments) < 2:
            return

        binish_idx = next(
            (i for i, seg in enumerate(segments[:-1]) if seg in _BINISH_SEGMENTS),
            None,
        )
        if binish_idx is None:
            return

        basename = segments[-1]
        base_dir_segments = segments[:-1]
        self.candidates.append((node.lineno, self._fn_stack[-1], "/".join([*base_dir_segments, basename])))


def find_dead_bin_references(
    root: Path,
    *,
    repo_root: Path | None = None,
    resolve_root: Path | None = None,
) -> list[tuple[str, int, str, str]]:
    """Walk ``root`` and return every ``(relpath, lineno, symbol,
    missing_target)`` tuple for a bin-ish path reference (see module
    docstring) whose resolved target does not exist under
    ``resolve_root``.

    ``repo_root`` (default ``_REPO_ROOT``) anchors reported relpaths.
    ``resolve_root`` (default same as ``repo_root``) is the filesystem root
    a recognised ``_REPO_ROOT``/``claude_klabauter_root``-convention Name is presumed
    to denote -- overridable so the plant-a-violation fixture tests can
    resolve against an isolated ``tmp_path`` tree instead of this real
    checkout.
    """
    repo_root = repo_root if repo_root is not None else _REPO_ROOT
    resolve_root = resolve_root if resolve_root is not None else repo_root

    violations: list[tuple[str, int, str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded_source_path(path):
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        visitor = _DeadBinRefVisitor()
        visitor.visit(tree)
        relpath = _relpath(path, repo_root)
        for lineno, symbol, target_relpath in visitor.candidates:
            if not _existing_candidate(resolve_root / Path(target_relpath).parent, Path(target_relpath).name):
                site = f"{relpath}::{symbol}"
                violations.append((relpath, lineno, site, target_relpath))
    return violations


def test_no_dead_bin_references_in_production_code():
    """Standing gate: every in-repo executable path referenced, by literal
    construction off a recognised repo-root name, from coordinator_core/
    production code must exist on disk at HEAD. A hit here means either a
    live instance of the deleted-binary class the motivating lesson names,
    or (if genuinely intentional) a resolution-rule narrowing to this file,
    reviewed like any other code change -- never a silent exemption entry
    (see module docstring's "No exemption set")."""
    violations = find_dead_bin_references(_SCAN_ROOT)
    assert violations == [], (
        "Found reference(s) to an in-repo bin-ish path that does not exist on disk "
        "(see state/lessons/2026-08-19-a-guard-on-a-deleted-binary-fails-silently-"
        f"forever.md for why this is break-class, not a false alarm): {violations}"
    )


def test_gate_detects_the_reconstructed_machine_local_incident(tmp_path):
    """Acceptance criterion: reconstruct the EXACT pre-fix condition this
    gate exists to catch -- ``first_run.py``'s
    ``machine_local_bin = claude_klabauter_root / "coordinator" / "bin" /
    "machine-local"`` with no such file (nor its .cmd/.ps1/.py twins) on
    disk -- and assert the gate flags it."""
    fixture = tmp_path / "fixture_machine_local.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "\n"
        "def _resolve_machine_local_argv(claude_klabauter_root):\n"
        '    machine_local_bin = claude_klabauter_root / "coordinator" / "bin" / "machine-local"\n'
        "    return machine_local_bin\n",
        encoding="utf-8",
    )
    # No coordinator/bin/machine-local (nor twins) created under tmp_path --
    # the reconstructed 2026-08-14-onward state.

    violations = find_dead_bin_references(tmp_path, repo_root=tmp_path, resolve_root=tmp_path)

    assert len(violations) == 1
    relpath, lineno, site, target = violations[0]
    assert relpath.endswith("fixture_machine_local.py")
    assert lineno == 4
    assert site.endswith("::_resolve_machine_local_argv")
    assert target == "coordinator/bin/machine-local"


def test_gate_does_not_flag_when_the_binary_exists(tmp_path):
    """Negative control paired with the reconstruction test: the identical
    shape, but with the target actually present, must not be flagged."""
    fixture = tmp_path / "fixture_machine_local_present.py"
    fixture.write_text(
        "def resolve(claude_klabauter_root):\n"
        '    return claude_klabauter_root / "coordinator" / "bin" / "machine-local"\n',
        encoding="utf-8",
    )
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "machine-local.cmd").write_text("@echo off\n", encoding="utf-8")

    violations = find_dead_bin_references(tmp_path, repo_root=tmp_path, resolve_root=tmp_path)

    assert violations == []


def test_gate_declines_a_non_repo_root_convention_name(tmp_path):
    """Negative control: the same bin-ish shape, rooted at a Name NOT in
    ``_REPO_ROOT_CONVENTION_NAMES`` (e.g. ``plugin_root``, denoting an
    install destination this repo's git history does not control), must be
    declined -- not flagged, regardless of whether the target exists."""
    fixture = tmp_path / "fixture_plugin_root.py"
    fixture.write_text(
        "def resolve(plugin_root):\n"
        '    return plugin_root / "bin" / "some-tool"\n',
        encoding="utf-8",
    )

    violations = find_dead_bin_references(tmp_path, repo_root=tmp_path, resolve_root=tmp_path)

    assert violations == []


def test_gate_declines_a_dynamic_basename_segment(tmp_path):
    """Negative control: a literal bin-ish directory joined with a
    non-literal (variable) final segment cannot be resolved statically and
    must be declined."""
    fixture = tmp_path / "fixture_dynamic_basename.py"
    fixture.write_text(
        "def resolve(repo_root, tool_name):\n"
        '    return repo_root / "coordinator" / "bin" / tool_name\n',
        encoding="utf-8",
    )

    violations = find_dead_bin_references(tmp_path, repo_root=tmp_path, resolve_root=tmp_path)

    assert violations == []


def test_gate_accepts_a_py_sibling_for_an_extensionless_reference(tmp_path):
    """Confirms the deliberate permissiveness named in the module docstring
    § point 4: an extensionless reference is NOT flagged when only a
    ``.py`` sibling exists (legitimate forwarder-resolution target), even
    though the bare file and its ``.cmd``/``.ps1`` twins are absent."""
    fixture = tmp_path / "fixture_py_sibling.py"
    fixture.write_text(
        "def resolve(claude_klabauter_root):\n"
        '    return claude_klabauter_root / "coordinator" / "bin" / "some-tool"\n',
        encoding="utf-8",
    )
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "some-tool.py").write_text("# stub\n", encoding="utf-8")

    violations = find_dead_bin_references(tmp_path, repo_root=tmp_path, resolve_root=tmp_path)

    assert violations == []


def test_gate_ignores_directory_only_references_and_os_path_join_shape(tmp_path):
    """Negative control: (a) a bin-ish reference with no basename segment
    after it (just the directory) is nothing to check existence of, and
    (b) the ``os.path.join`` shape resolves and flags identically to the
    ``BinOp(Div)`` shape when the target is genuinely missing."""
    fixture = tmp_path / "fixture_join_shape.py"
    fixture.write_text(
        "import os\n"
        "\n"
        "def bin_dir(repo_root):\n"
        '    return repo_root / "coordinator" / "bin"\n'
        "\n"
        "def joined(repo_root):\n"
        '    return os.path.join(repo_root, "coordinator", "bin", "missing-tool.py")\n',
        encoding="utf-8",
    )

    violations = find_dead_bin_references(tmp_path, repo_root=tmp_path, resolve_root=tmp_path)

    assert len(violations) == 1
    relpath, lineno, site, target = violations[0]
    assert site.endswith("::joined")
    assert target == "coordinator/bin/missing-tool.py"


def test_gate_does_not_flag_a_referenced_subdirectory(tmp_path):
    """Negative control found during real-tree verification:
    ``coordinator_core/install/install_health_run.py`` and
    ``coordinator_core/benchmarks/shim_prototype_dispatcher.py`` both join a
    recognised repo-root name with "coordinator"/"bin" plus a final literal
    segment (``install-health``, ``lib``) that names a real SUBDIRECTORY,
    not a file. Without the ``is_dir()`` rung in ``_existing_candidate``
    this fixture would false-positive; with it, it must not."""
    fixture = tmp_path / "fixture_subdir_reference.py"
    fixture.write_text(
        "def resolve(repo_root):\n"
        '    return repo_root / "coordinator" / "bin" / "lib"\n',
        encoding="utf-8",
    )
    (tmp_path / "coordinator" / "bin" / "lib").mkdir(parents=True)

    violations = find_dead_bin_references(tmp_path, repo_root=tmp_path, resolve_root=tmp_path)

    assert violations == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
