"""
coordinator_core.tests.test_session_dir_has_one_constructor — the artifact
that discharges the one-constructor rule: no module in this corpus may
``mkdir`` a session directory except ``session/core.py::init``, the body of
``session/core.py::ensure_session``.

Why this guard exists, in facts. ``init`` is the only writer of
``meta.json``, and it used to run LAZILY — from whichever bookkeeping writer
happened to reach the session hub first. Meanwhile a dozen writers reached a
session directory into being with ``mkdir(parents=True, exist_ok=True)`` and
dropped their own file in it. Which sessions got a record was therefore a RACE
between lazy initializers, and a session that lost it was not degraded but
INVISIBLE: every ``update_meta_field`` write silently no-ops (False on an
absent file, by contract), ``goal`` has no harness-registry substitute so the
session renders to every peer as ``holder_goal_state: undeclared``, and
``ops/session/reap.py`` fail-closes to KEEP a directory it cannot read, so the
session is both invisible and unreapable, accumulating forever. Observed
directly on this box 2026-08-26: sessions born 13:47 and 13:49 had no
``meta.json`` minutes later while one born 13:51 did.

``ensure_session`` fixed the ~dozen writers that existed on 2026-08-26. This
test is what stops the next one, which by construction is not one anyone
thought to fix. Without it the defect regrows: a new writer that needs a
session directory reaches for the same two-line ``mkdir`` every one of the
converted sites used, and nothing about the result looks wrong until a peer
asks what a live holder is doing.

Why AST, not grep
==================
A substring grep over this corpus returns overwhelmingly comments, docstrings,
and prose — including this module's own. The scan below walks for ``mkdir`` /
``os.makedirs`` CALL nodes and reconstructs each target's path recipe by
following simple local assignments, so only a real, materialized path
expression is ever inspected. This mirrors ``test_no_legacy_touch_record_
literal.py`` and ``test_no_bare_chain_terminal_literal.py``'s existing AST-gate
discipline in this same corpus, not a novel technique.

What counts as a violation, precisely
======================================
A ``mkdir``/``makedirs`` whose target resolves to **the session hub plus
exactly one non-literal component** — i.e. ``<hub>/<sid>`` for a ``sid`` that
is a variable. The hub is recognised two ways: a literal
``"coordinator-sessions"`` component, or a call to one of the hub resolvers
(``sessions_dir``, ``_sessions_dir``). A call to ``session_dir(...)`` resolves
to a session directory outright.

Deliberately NOT violations, because each is a different corpus or a
literal-named sibling that is not a session:

  - a LITERAL-named child of the hub (``logs``, ``.archive``, ``.agents``,
    ``no-session``). ``liveness._NON_SESSION_DIR_NAMES`` denylists these; they
    are hub bookkeeping, not sessions. The audit writers converted alongside
    this guard land in ``no-session`` for exactly that reason.
  - the hub ITSELF (``ops/scope_soak_enable.py``) — creating the hub creates
    no session.
  - ``state/subagent-share/`` per sid, the settings home's write-bump
    anchor and context-window sidecar, and the tempdir guard-unlock sentinels.
    All three key a directory on a session id and none of them is a session;
    minting a ``meta.json`` for any of them would be the inverse defect.
  - a deeper path under an already-existing session dir (``<hub>/<sid>/
    hook-emits``) — its parent's existence is the caller's problem and is
    guarded at that caller, not here.
"""

from __future__ import annotations

import ast
import pathlib
from typing import List, Optional, Tuple

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Scanned trees. ``coordinator/bin`` is included because
#: ``coordinator-safe-commit.py`` was one of the converted sites — a guard that
#: only watched the package would have left the ceremony free to regrow it.
_SCAN_ROOTS = ("coordinator_core", "coordinator/bin")

#: Resolver calls that answer with the session HUB.
_HUB_RESOLVERS = frozenset({"sessions_dir", "_sessions_dir"})

#: Resolver calls that answer with a SESSION DIRECTORY outright.
_SESSION_DIR_RESOLVERS = frozenset({"session_dir"})

#: Marker components used by ``_recipe``.
_HUB = ("<hub>",)
_SESSION = ("<session-dir>",)
_OPAQUE = ("<opaque>",)

#: The one legitimate constructor, plus the ONLY other site allowed to create a
#: session-id-named child of the hub. Every entry is ``relpath::symbol``, named
#: and dated, and is re-validated by
#: ``test_named_exemption_still_describes_a_real_site`` — an entry that stops
#: describing a real mkdir is stale and must be deleted, so a future refactor
#: re-trips this gate instead of riding a dead exemption forever.
_EXEMPT_SITES = {
    # 2026-08-26 — THE constructor. `ensure_session` delegates its create to
    # this function; it is the single point this whole guard exists to funnel
    # every other writer through.
    "coordinator_core/session/core.py::init",
    # 2026-08-26 — self-probe fixture. Builds a back-pointer chain inside the
    # throwaway repo `_scratch_git_repo()` yields, never the live hub; its
    # `git_root` argument is that scratch repo (call site, `_trigger_*`).
    "coordinator_core/bash_guards/_alternative_liveness.py::_make_backpointer",
}


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    leaf = parts[-1]
    return (
        "tests" in parts
        or "test" in parts
        or leaf.startswith("test_")
        or leaf.endswith("_test.py")
        or leaf == "conftest.py"
    )


class _Recipe:
    """A resolved path expression as an ordered list of components.

    Each component is either a ``str`` (a string literal in the path) or one of
    the marker tuples above (``_HUB``, ``_SESSION``, ``_OPAQUE``).
    """


def _literal(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _recipe(node: ast.AST, scope: dict, depth: int = 0) -> List[object]:
    """Resolve ``node`` to a list of path components, following local
    assignments up to a bounded depth (an unbounded walk on a cyclic or
    self-referential assignment would not terminate)."""
    if depth > 6:
        return [_OPAQUE]

    lit = _literal(node)
    if lit is not None:
        # A literal path may itself carry separators.
        return [p for p in lit.replace("\\", "/").split("/") if p]

    if isinstance(node, ast.IfExp):
        # `base = sessions_base if sessions_base else sessions_dir(cwd)` --
        # both branches are real answers; take whichever resolves to the hub so
        # a pre-resolved seam cannot hide a session-dir mkdir behind a ternary.
        for branch in (node.body, node.orelse):
            resolved = _recipe(branch, scope, depth + 1)
            if _hub_index(resolved) is not None:
                return resolved
        return _recipe(node.body, scope, depth + 1)

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _recipe(node.left, scope, depth + 1) + _recipe(node.right, scope, depth + 1)

    if isinstance(node, ast.Call):
        name = _callee_name(node)
        if name in _HUB_RESOLVERS:
            return [_HUB]
        if name in _SESSION_DIR_RESOLVERS:
            return [_SESSION]
        if name == "join":  # os.path.join(...)
            out: List[object] = []
            for arg in node.args:
                out.extend(_recipe(arg, scope, depth + 1))
            return out
        if name in ("Path", "str"):
            if node.args:
                return _recipe(node.args[0], scope, depth + 1)
            return [_OPAQUE]
        if name == "joinpath":
            out = _recipe(node.func.value, scope, depth + 1) if isinstance(node.func, ast.Attribute) else [_OPAQUE]
            for arg in node.args:
                out.extend(_recipe(arg, scope, depth + 1))
            return out
        return [_OPAQUE]

    if isinstance(node, ast.Attribute):
        if node.attr == "parent":
            base = _recipe(node.value, scope, depth + 1)
            return base[:-1] if base else [_OPAQUE]
        return [_OPAQUE]

    if isinstance(node, ast.Name):
        bound = scope.get(node.id)
        if bound is not None:
            return _recipe(bound, scope, depth + 1)
        return [_OPAQUE]

    return [_OPAQUE]


def _hub_index(recipe: List[object]) -> Optional[int]:
    for i, comp in enumerate(recipe):
        if comp is _HUB or comp == "coordinator-sessions":
            return i
    return None


def _is_session_dir_recipe(recipe: List[object]) -> bool:
    """True iff ``recipe`` names ``<hub>/<sid>`` with a NON-literal ``sid``, or
    a bare ``session_dir(...)`` resolution."""
    if not recipe:
        return False
    if recipe[-1] is _SESSION:
        return True
    idx = _hub_index(recipe)
    if idx is None:
        return False
    tail = recipe[idx + 1 :]
    if len(tail) != 1:
        return False  # the hub itself, or something deeper under a session dir
    return not isinstance(tail[0], str)  # a literal-named hub child is not a session


class _MkdirVisitor(ast.NodeVisitor):
    """Collect ``(lineno, symbol)`` for every mkdir of a session directory."""

    def __init__(self) -> None:
        self.violations: List[Tuple[int, str]] = []
        self._symbol = "<module>"
        self._scope: dict = {}

    def _visit_scope(self, node) -> None:
        outer_symbol, outer_scope = self._symbol, self._scope
        self._symbol = node.name
        self._scope = dict(outer_scope)
        self.generic_visit(node)
        self._symbol, self._scope = outer_symbol, outer_scope

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_ClassDef(self, node) -> None:
        self._visit_scope(node)

    def visit_Assign(self, node) -> None:
        self.generic_visit(node)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._scope[target.id] = node.value

    def visit_AnnAssign(self, node) -> None:
        self.generic_visit(node)
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._scope[node.target.id] = node.value

    def visit_Call(self, node) -> None:
        self.generic_visit(node)
        name = _callee_name(node)
        target: Optional[ast.AST] = None
        if name == "mkdir" and isinstance(node.func, ast.Attribute):
            target = node.func.value
        elif name == "makedirs" and node.args:
            target = node.args[0]
        if target is None:
            return
        if _is_session_dir_recipe(_recipe(target, self._scope)):
            self.violations.append((node.lineno, self._symbol))


def find_session_dir_mkdirs(root: pathlib.Path, repo_root: pathlib.Path) -> List[str]:
    """Return ``relpath:lineno::symbol`` for every non-exempt session-directory
    mkdir under ``root``. Exposed (not private) so the negative-control tests
    below can drive it against a synthetic tree."""
    found: List[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if _is_test_path(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        visitor = _MkdirVisitor()
        visitor.visit(tree)
        for lineno, symbol in visitor.violations:
            if f"{rel}::{symbol}" in _EXEMPT_SITES:
                continue
            found.append(f"{rel}:{lineno}::{symbol}")
    return found


def test_no_module_mkdirs_a_session_directory():
    """The gate. Every session directory on this machine is created by
    ``session/core.py::ensure_session``, which produces the directory and its
    ``meta.json`` record together or neither."""
    violations: List[str] = []
    for scan_root in _SCAN_ROOTS:
        root = _REPO_ROOT / scan_root
        if root.is_dir():
            violations.extend(find_session_dir_mkdirs(root, _REPO_ROOT))

    assert violations == [], (
        "These sites mkdir a session directory without its meta.json record. "
        "Call coordinator_core.session.core.ensure_session(sid, cwd) instead — "
        "it produces the directory and the record together or neither. A site "
        "that must NOT create a session (an audit log, a probe fixture) takes "
        "the denylisted `no-session` bucket via "
        "bash_guards._override_log_path.session_audit_log_dir, or is added to "
        "_EXEMPT_SITES with a dated reason.\n  " + "\n  ".join(violations)
    )


def test_a_new_bare_mkdir_of_a_session_dir_is_caught(tmp_path):
    """Negative control: the gate must actually fire on the shape every
    converted site used, or it is decoration."""
    mod = tmp_path / "newwriter.py"
    mod.write_text(
        "from pathlib import Path\n"
        "def write_something(git_root, session_id):\n"
        "    sdir = Path(git_root) / '.git' / 'coordinator-sessions' / session_id\n"
        "    sdir.mkdir(parents=True, exist_ok=True)\n",
        encoding="utf-8",
    )
    found = find_session_dir_mkdirs(tmp_path, tmp_path)
    assert found == ["newwriter.py:4::write_something"]


def test_the_os_makedirs_spelling_is_caught_too(tmp_path):
    """Half the converted sites spelled it ``os.makedirs``, resolved through a
    ``session_dir()`` call rather than a literal hub component."""
    mod = tmp_path / "otherwriter.py"
    mod.write_text(
        "import os\n"
        "from coordinator_core.session.core import session_dir\n"
        "def write_something(sid, cwd):\n"
        "    sdir = session_dir(sid, cwd)\n"
        "    os.makedirs(sdir, exist_ok=True)\n",
        encoding="utf-8",
    )
    found = find_session_dir_mkdirs(tmp_path, tmp_path)
    assert found == ["otherwriter.py:5::write_something"]


def test_a_literal_named_hub_child_is_not_flagged(tmp_path):
    """Second half, and it matters as much as the first: ``logs``,
    ``.archive``, ``.agents`` and ``no-session`` are denylisted hub bookkeeping
    (``liveness._NON_SESSION_DIR_NAMES``), not sessions. A guard that flagged
    them would be turned off within the day."""
    mod = tmp_path / "hubchild.py"
    mod.write_text(
        "from pathlib import Path\n"
        "def write_log(common_dir):\n"
        "    log_dir = common_dir / 'coordinator-sessions' / 'logs'\n"
        "    log_dir.mkdir(parents=True, exist_ok=True)\n"
        "def write_hub(common_dir):\n"
        "    hub = common_dir / 'coordinator-sessions'\n"
        "    hub.mkdir(parents=True, exist_ok=True)\n",
        encoding="utf-8",
    )
    assert find_session_dir_mkdirs(tmp_path, tmp_path) == []


def test_a_sibling_corpus_keyed_on_a_session_id_is_not_flagged(tmp_path):
    """``state/subagent-share/<sid>`` and the settings-home sidecars key a
    directory on a session id and are NOT sessions. Routing them through
    ``ensure_session`` would mint a session record for something that is not a
    session — the inverse defect, not a stricter version of the same one."""
    mod = tmp_path / "sibling.py"
    mod.write_text(
        "from pathlib import Path\n"
        "def provision(git_root, sid):\n"
        "    d = Path(git_root) / 'state' / 'subagent-share' / sid\n"
        "    d.mkdir(parents=True, exist_ok=True)\n",
        encoding="utf-8",
    )
    assert find_session_dir_mkdirs(tmp_path, tmp_path) == []


def test_a_test_module_is_never_scanned(tmp_path):
    """Fixtures legitimately build session dirs by hand in their own tmp trees.
    The live-hub litter they used to leave is a different guard's job
    (``conftest``'s ``_no_new_live_session_hub_entries``, pinned by
    ``test_live_session_hub_litter_guard.py``)."""
    pkg = tmp_path / "tests"
    pkg.mkdir()
    (pkg / "test_thing.py").write_text(
        "from pathlib import Path\n"
        "def test_x(tmp_path, sid):\n"
        "    d = tmp_path / 'coordinator-sessions' / sid\n"
        "    d.mkdir(parents=True, exist_ok=True)\n",
        encoding="utf-8",
    )
    assert find_session_dir_mkdirs(tmp_path, tmp_path) == []


def test_named_exemption_still_describes_a_real_site():
    """Self-invalidating exemption (mirrors ``test_no_legacy_touch_record_
    literal.py``'s ``_PRODUCTION_EXEMPT_SITES`` discipline): each entry must
    still name a symbol that actually contains a session-directory mkdir at
    HEAD. An entry that no longer describes a real hit is stale and must be
    deleted, so a future refactor that moves or removes the mkdir re-trips this
    gate instead of riding a dead exemption forever."""
    stale: List[str] = []
    for site in sorted(_EXEMPT_SITES):
        relpath, _, symbol = site.partition("::")
        path = _REPO_ROOT / relpath
        if not path.is_file():
            stale.append(f"{site} (file no longer exists)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _MkdirVisitor()
        visitor.visit(tree)
        if not any(sym == symbol for _lineno, sym in visitor.violations):
            stale.append(f"{site} (no matching session-dir mkdir found)")

    assert stale == [], f"Stale _EXEMPT_SITES entry(ies) — delete them: {stale}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
