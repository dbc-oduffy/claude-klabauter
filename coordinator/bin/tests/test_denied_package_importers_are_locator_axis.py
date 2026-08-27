"""A CLI importing a publish-DENIED engine package resolves the tree that has it.

Sibling to `test_precommit_trampoline_engine_axis.py`, same axis defect, other
cause. That file guards the modules publish RENAMES; this one guards the
packages publish REFUSES TO SHIP AT ALL. In both, a `coordinator/bin` CLI
resolves its engine on the DISPATCH axis -- "which engine executes on this box",
answered by `_resolve_claude_klabauter_root()`, whose own docstring says it "reaches the
published mirror through the pointer-file/registry rung" -- and then imports a
`coordinator_core` name that the mirror, correctly, does not contain.

The denial is not an oversight to be waived: `setup/publish-allowlist-
declarations.yaml`'s engine-row `deny` list is deny-by-default and ratified
(docs/reference/publish-allowlist-derivation.md, AC10). `percolate` and
`publish` are publisher-side machinery; a released engine has no business
carrying the tool that released it. So the import and the axis cannot both be
right, and the axis is the half that is wrong.

Measured 2026-08-26, three sites, all failing unconditionally from any cwd that
did not happen to put the working tree on `sys.path` first:

    verify-publish-targets-portable-sync.py  coordinator_core.percolate.runtime_root
    check-persona-slug-leak.py               coordinator_core.publish.time_transform
    publish-time-transform-py.py             coordinator_core.publish.time_transform

The first had been dying before it reached any of its own assertions, so the
allowlist-rot check it fronts had silently not run for as long as the defect
stood -- a green-looking probe that was executing none of its subject.

WHY THE ACCIDENT HID IT. Invoked as `python coordinator/bin/<cli>.py` from the
repo root, `sys.path[0]` is the script's own directory and the cwd is on the
path, so `coordinator_core` binds from the working tree before the dispatch
front-insert can matter. Every one of these worked when run the way a developer
runs them and failed the way a hook or a probe runs them.

WHAT THIS DOES NOT ASSERT. Not "no CLI may use the dispatch axis" -- that axis
is correct for the ~200 CLIs whose ops ship to the mirror, and routing them onto
the locator seam would repoint the fleet at the working tree (see
`cc_invoke.require_dispatch_engine_on_path`'s own "WHY NOT REUSE" paragraph).
The rule is narrow: dispatch axis AND an import of a denied package is the
contradiction. Either alone is fine.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"
_REPO_ROOT = _TESTS_DIR.parents[2]

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke  # noqa: E402  (import after path setup)

_DECLARATIONS = _REPO_ROOT / "setup" / "publish-allowlist-declarations.yaml"

_ENGINE_ROW = "claude-klabauter"

_DISPATCH_SEAM = "require_dispatch_engine_on_path"
_COLOCATED_SEAM = "require_colocated_engine_on_path"


def _calls_seam(source: str, seam: str) -> bool:
    """True when `source` CALLS `seam` -- never when it merely mentions one.

    AST, for the same reason `_imported_coordinator_core_tops` is AST: a
    `\\bname\\s*\\(` regex reads `require_dispatch_engine_on_path()` written
    inside a comment or docstring as a call, and every file this guard has
    already fixed names the dispatch seam in the prose explaining why it is
    off it. `percolate-round.py` was reported as an offender on exactly that
    text while making no such call.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a broken file is another test's finding
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if name == seam:
            return True
    return False


def _denied_top_level_packages() -> set[str]:
    """The engine row's `deny` entries that are importable Python names.

    Read from the declarations yaml, never restated here: the deny list is
    hand-authored and moves, and a copy in this file would go quietly stale in
    the safe-looking direction (a newly denied package no longer guarded).
    Dotfiles (`.gitignore`, `.percolate-ignore`) and the two denied test modules
    are dropped -- nothing imports them as a package.

    Returns an empty set when the declarations file is absent (the published
    mirror does not carry it), so this degrades to a skip there rather than
    erroring on a missing file.
    """
    if not _DECLARATIONS.is_file():
        return set()
    parsed = yaml.safe_load(_DECLARATIONS.read_text(encoding="utf-8"))
    row = _find_row(parsed, _ENGINE_ROW)
    assert row is not None, (
        f"{_DECLARATIONS.name} no longer carries a '{_ENGINE_ROW}' row — this guard "
        "reads its deny list as the source of truth and must be repointed, not deleted"
    )
    names = {entry["name"] if isinstance(entry, dict) else entry for entry in row["deny"]}
    return {n[:-3] if n.endswith(".py") else n for n in names if not n.startswith(".")} - {
        n[:-3] for n in names if isinstance(n, str) and n.startswith("test_")
    }


def _find_row(node: object, wanted: str) -> dict | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == wanted and isinstance(value, dict):
                return value
            found = _find_row(value, wanted)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_row(value, wanted)
            if found is not None:
                return found
    return None


def _imported_coordinator_core_tops(source: str) -> set[str]:
    """Top-level `coordinator_core.<name>` packages this source imports.

    AST, not regex: these imports sit inside functions as often as at module
    level (deferring the import past the resolve call is the whole shape), and a
    line-anchored regex would miss the indented ones — which are exactly the
    ones this defect lives in.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a broken file is another test's finding
        return set()
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "coordinator_core" and len(parts) > 1:
                tops.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "coordinator_core" and len(parts) > 1:
                    tops.add(parts[1])
    return tops


def _offenders() -> list[tuple[Path, str, set[str]]]:
    denied = _denied_top_level_packages()
    found: list[tuple[Path, str, set[str]]] = []
    for script in sorted(_BIN_DIR.glob("*.py")):
        source = script.read_text(encoding="utf-8", errors="replace")
        if not _calls_seam(source, _DISPATCH_SEAM):
            continue
        hit = _imported_coordinator_core_tops(source) & denied
        if hit:
            found.append((script, source, hit))
    return found


def test_no_cli_dispatches_to_an_engine_that_cannot_hold_its_import() -> None:
    """AC-axis: dispatch axis and a publish-denied import are mutually exclusive.

    Box-independent — reads the declarations yaml, not this machine's engine-root
    registry — so it fails on a publisher box and a stranger's box alike, and it
    catches the NEXT CLI written by copying the nearest sibling.
    """
    if not _denied_top_level_packages():
        pytest.skip("allowlist declarations absent (published mirror) — nothing to cross-check")

    offenders = [f"{script.name} -> {sorted(hit)}" for script, _source, hit in _offenders()]
    assert not offenders, (
        "these CLIs resolve their engine on the DISPATCH axis and then import a "
        "coordinator_core package the engine row permanently DENIES to the published "
        "mirror — on any box whose dispatch root is that mirror the import fails "
        "unconditionally: "
        + "; ".join(sorted(offenders))
        + f". Route them onto the locator axis ({_COLOCATED_SEAM}(__file__)) with the "
        "reason named at the call site, as verify-publish-targets-portable-sync.py does."
    )


@pytest.mark.parametrize(
    "cli_name",
    [
        "verify-publish-targets-portable-sync.py",
        "check-persona-slug-leak.py",
        "publish-time-transform-py.py",
    ],
)
def test_the_three_measured_clis_resolve_a_root_holding_their_imports(cli_name: str) -> None:
    """AC-import: the root each one actually resolves contains what it imports.

    The named regression, against the real ladder rather than against the rule.
    Box-SENSITIVE by construction — it exercises the resolver, which is a
    property of the box — and that is the point: this is the assertion that was
    false on the machine where the defect was found.

    The axis is discovered from the source, never assumed, so this cannot go
    green by checking a seam the file no longer calls.
    """
    script = _BIN_DIR / cli_name
    if not script.is_file():
        pytest.skip(f"{cli_name} absent from this tree")
    source = script.read_text(encoding="utf-8", errors="replace")

    if _calls_seam(source, _COLOCATED_SEAM):
        root = Path(cc_invoke.resolve_colocated_claude_klabauter_root(str(script)))
    elif _calls_seam(source, _DISPATCH_SEAM):
        root = Path(cc_invoke._resolve_claude_klabauter_root())
    else:  # pragma: no cover - a third axis would need its own reasoning here
        pytest.fail(
            f"{cli_name} resolves its engine root through neither known seam; this "
            "test must be taught the new one rather than deleted."
        )

    denied = _denied_top_level_packages()
    imported = _imported_coordinator_core_tops(source)
    assert imported, f"{cli_name} imports no coordinator_core package"

    for package in sorted(imported & denied) or sorted(imported):
        target = root / "coordinator_core" / package
        assert target.is_dir() or target.with_suffix(".py").is_file(), (
            f"{cli_name} imports coordinator_core.{package}, but the engine root it "
            f"resolves ({root}) does not contain it — the import fails at run time. "
            "This is the published-mirror-vs-working-tree axis defect: the package is "
            "denied to the mirror, so it is only importable from the checkout this CLI "
            "itself ships in."
        )
