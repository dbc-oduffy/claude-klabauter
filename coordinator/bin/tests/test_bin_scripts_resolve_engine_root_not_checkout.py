"""No coordinator/bin script resolves `coordinator_core` from the checkout it lives in.

A script that front-inserts `Path(__file__).resolve().parents[n]` (or a `_REPO_ROOT` /
`HERE.parents[1]` name bound to it), or hands that path to `place_engine_root_on_path`, imports
the engine from its own checkout. In a non-engine mirror (DoE's plugin root, the example-game-repo tree)
that checkout has no `coordinator_core`, so the script dies with ModuleNotFoundError. The engine
root must come from `cc_invoke.ensure_engine_on_path` / `require_engine_on_path`, or from a
resolver call whose result is passed on.

Static, AST-only: nothing is imported or run. A path argument is a checkout-root pin when,
after following module-level name bindings, it derives from `__file__` through `parents[n>=1]`,
`.parent.parent` or nested `dirname`. Sibling inserts (`.parent`, anything `/ "lib"`) are not
the pin. `_UNMIGRATED` is a ratchet: sites not yet moved onto the resolver; an entry that no
longer pins must be deleted, and a new pin cannot be added to it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BIN = _REPO_ROOT / "coordinator" / "bin"

_UNMIGRATED: frozenset[str] = frozenset({
    "aggregate-rollup.py",
    "coordinator-settings-home.py",
    "coordinator-subsession.py",
    "detect-initiative-candidates.py",
    "door-serving-census.py",
    "emit-guard-enforcement-join.py",
    "fast-tier-budget.py",
    "freeze-review-diff.py",
    "gen-launcher-shim.py",
    "group-em-memory-governor.py",  # migration rides with a peer's uncommitted fleet hunks
    "generate-claudemeta-manifest.py",
    "goal-kr-evidence.py",
    "grok-coordinator.py",
    "handoff-housekeeping.py",
    "judge-coverage-backfill.py",
    "klabauter-channel.py",
    "landed-tree-test-verify.py",
    "lib/gem_save_box.py",
    "lib/repo_identity.py",
    "list-orphaned-plans.py",
    "measure-amplification-discriminator.py",
    "model-for-role.py",
    "parallel-review-gate-decision.py",
    "percolate-parse-dryrun.py",
    "percolate-round.py",
    "pre_commit_corpus_artifact_guard.py",
    "prune-closed-improvements.py",
    "publish-repo-bundle-at-ceremony.py",
    "publish.py",
    "red-set-report.py",
    "render-ceremony-receipt.py",
    "repo-setup-args-and-register.py",
    "resolve-repo-path.py",
    "resolve-session-address.py",
    "scip-rebuild-at-ceremony.py",
    "snippet-registry.py",
    "sync-shared-index.py",
    "unimatrix-codex-launch.py",
    "update-docs-probes.py",
    "verify-snippet-sync.py",
    "wait-for-count.py",
    "warm-engine-stop.py",
    "warm-serve-partition.py",
    "workday-start-health-probes.py",
    "writes-ignored-check.py",
    "wsc-coverage-gate-runner.py",
})


def _unwrap(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name in {"str", "fspath", "resolve", "abspath", "realpath"}:
            node = node.args[0]
        else:
            break
    return node


def _is_subpath(node: ast.AST) -> bool:
    """`<x> / "lib"` or `os.path.join(<x>, "lib")`: a named child, never a checkout root."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        return isinstance(right, ast.Constant) and isinstance(right.value, str) and right.value != ".."
    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "join" and len(node.args) >= 2:
        last = node.args[-1]
        return isinstance(last, ast.Constant) and isinstance(last.value, str) and last.value != ".."
    return False


def _bindings(tree: ast.AST) -> dict[str, list[ast.AST]]:
    bound: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            bound.setdefault(node.target.id, []).append(node.value)
    return bound


def _climbs(node: ast.AST, bound: dict[str, list[ast.AST]], seen: frozenset[str]) -> bool:
    """True when `node` derives from `__file__` and climbs at least two levels above it."""
    if isinstance(node, ast.Name):
        if node.id in seen:
            return False
        return any(_climbs(v, bound, seen | {node.id}) for v in bound.get(node.id, []))
    depth, derived = _walk(node, bound, seen)
    return derived and depth >= 2


def _walk(node: ast.AST, bound: dict[str, list[ast.AST]], seen: frozenset[str]) -> tuple[int, bool]:
    """(levels climbed above `__file__`, whether the chain reaches `__file__`)."""
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return 0, True
        if node.id in seen:
            return 0, False
        best = (0, False)
        for value in bound.get(node.id, []):
            got = _walk(value, bound, seen | {node.id})
            if got[1] and (not best[1] or got[0] > best[0]):
                best = got
        return best
    if isinstance(node, ast.Attribute):
        depth, derived = _walk(node.value, bound, seen)
        return (depth + 1 if node.attr == "parent" else depth), derived
    if isinstance(node, ast.Subscript):
        depth, derived = _walk(node.value, bound, seen)
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "parents":
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, int):
                depth, derived = _walk(value.value, bound, seen)
                return depth + index.value + 1, derived
        return depth, derived
    if isinstance(node, ast.Call):
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name == "dirname" and node.args:
            depth, derived = _walk(node.args[0], bound, seen)
            return depth + 1, derived
        if name in {"Path", "resolve", "abspath", "realpath", "str", "fspath"}:
            target = node.args[0] if node.args else getattr(fn, "value", None)
            if target is not None:
                return _walk(target, bound, seen)
        if isinstance(fn, ast.Attribute):
            return _walk(fn.value, bound, seen)
    return 0, False


def _is_path_mutation(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Attribute) and fn.attr in {"insert", "append"}:
        owner = fn.value
        return isinstance(owner, ast.Attribute) and owner.attr == "path"
    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
    return name == "place_engine_root_on_path"


def find_checkout_root_pins(source: str) -> list[int]:
    """Line numbers where `source` front-inserts a `__file__`-derived checkout root."""
    if "coordinator_core" not in source:
        return []
    tree = ast.parse(source)
    bound = _bindings(tree)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_path_mutation(node) or not node.args:
            continue
        path_arg = _unwrap(node.args[-1])
        if _is_subpath(path_arg):
            continue
        if _climbs(path_arg, bound, frozenset()):
            lines.append(node.lineno)
    return sorted(lines)


def _bin_sources() -> list[Path]:
    return sorted(
        p for p in _BIN.rglob("*.py")
        if not {"tests", "__pycache__", "test-fixtures"} & set(p.parts)
        and not p.name.startswith("test_")
    )


def _pinned_files() -> dict[str, list[int]]:
    found = {}
    for path in _bin_sources():
        lines = find_checkout_root_pins(path.read_text(encoding="utf-8"))
        if lines:
            found[path.relative_to(_BIN).as_posix()] = lines
    return found


def test_no_bin_script_pins_its_checkout_root_for_the_engine():
    new = {rel: lines for rel, lines in _pinned_files().items() if rel not in _UNMIGRATED}
    assert new == {}, (
        "coordinator/bin script front-inserts a __file__-derived checkout root; in a non-engine "
        "mirror coordinator_core is not there. Resolve with cc_invoke.ensure_engine_on_path(__file__) "
        "(or require_engine_on_path):\n  " + "\n  ".join(f"{rel}:{lines}" for rel, lines in new.items())
    )


def test_unmigrated_ratchet_lists_only_files_that_still_pin():
    stale = sorted(_UNMIGRATED - set(_pinned_files()))
    assert stale == [], f"no longer pins; delete from _UNMIGRATED: {stale}"


def test_a_planted_parents_pin_is_reported():
    source = (
        "import sys\nfrom pathlib import Path\n"
        "_REPO_ROOT = Path(__file__).resolve().parents[2]\n"
        "sys.path.insert(0, str(_REPO_ROOT))\n"
        "from coordinator_core.x import y\n"
    )
    assert find_checkout_root_pins(source) == [4]


def test_a_planted_place_engine_root_on_a_parents_name_is_reported():
    source = (
        "from pathlib import Path\n"
        "HERE = Path(__file__).resolve().parent\n"
        "def f():\n"
        "    place_engine_root_on_path(str(HERE.parents[1]))\n"
        "    import coordinator_core\n"
    )
    assert find_checkout_root_pins(source) == [4]


def test_a_planted_double_dirname_pin_is_reported():
    source = (
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "import coordinator_core\n"
    )
    assert find_checkout_root_pins(source) == [2]


def test_sibling_lib_inserts_and_resolved_roots_are_allowed():
    source = (
        "import os, sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))\n"
        "HERE = Path(__file__).resolve().parent\n"
        "sys.path.insert(0, str(HERE))\n"
        "root = resolve_engine_root()\n"
        "sys.path.insert(0, root)\n"
        "place_engine_root_on_path(root)\n"
        "cc_invoke.ensure_engine_on_path(__file__)\n"
        "import coordinator_core\n"
    )
    assert find_checkout_root_pins(source) == []


def test_a_script_that_never_names_the_engine_is_not_swept():
    source = "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
    assert find_checkout_root_pins(source) == []
