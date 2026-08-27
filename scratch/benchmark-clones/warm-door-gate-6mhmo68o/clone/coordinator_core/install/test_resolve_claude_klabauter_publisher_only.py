"""
coordinator_core.install.test_resolve_claude_klabauter_publisher_only — keeps
``_resolve_claude_klabauter.PUBLISHER_ONLY_TARGETS`` honest against what
``coordinator/bin/`` actually imports.

Why this file exists. ``resolve_claude_klabauter_root_with_class()`` diverts any
session whose own repo root is not the live claude-klabauter checkout to the published
engine mirror. The mirror is the publish DESTINATION: it carries every
``coordinator/bin/`` FILENAME (C13 closed the per-name gap deliberately) but
not ``coordinator_core.percolate.*`` / ``coordinator_core.ops.percolate_run``,
which are publisher-side only. A percolate driver resolved from there passes
every structural check ``exec_cli`` runs — root exists, ``coordinator/bin``
exists, sentinel present, target file present — and then dies on an import
that can never succeed, with ``--dry-run`` returning before the failing
import and so reporting clean. Observed live 2026-08-20:
``state/bug-backlog/2026-08-20-coordinator-publish-is-unusable-from-ins-5feb28440ac1.yaml``.

``exec_cli`` fixes that by resolving those targets live-tree-only. The set
naming them is hand-maintained inside ``_resolve_claude_klabauter.py`` because that
module is installed standalone into a bare ``bin/`` with only the stdlib
importable — it cannot import the engine to derive the set itself. This test
is the artifact that discharges the maintenance: it re-derives the set from
``coordinator/bin/``'s real imports and fails when the two disagree, so a new
percolate-dispatching CLI cannot ship silently broken for every non-claude-klabauter
session on the box.

Negative-spec: an over-broad set is a real failure too, not a safe default —
a target wrongly listed here stops resolving from the published engine on
boxes that legitimately have no live claude-klabauter checkout. Both directions are
asserted.

Spec backlink: state/bug-backlog/2026-08-20-coordinator-publish-is-unusable-from-ins-5feb28440ac1.yaml
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"

_spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_under_test_publisher_only", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
resolve_claude_klabauter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = resolve_claude_klabauter
_spec.loader.exec_module(resolve_claude_klabauter)


#: The publisher-side engine surface the mirror deliberately does not ship.
#: Matching on the dotted prefix rather than a full import line catches both
#: ``from coordinator_core.ops.percolate_run import ...`` and the
#: ``importlib``/string-literal spellings the drivers also use.
_PUBLISHER_ONLY_MODULE_TOKENS = (
    "coordinator_core.percolate",
    "coordinator_core.ops.percolate_run",
)


def _direct_importers() -> set[str]:
    """``coordinator/bin/*.py`` filenames that name a publisher-only module."""
    found = set()
    for path in sorted(_BIN_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(token in text for token in _PUBLISHER_ONLY_MODULE_TOKENS):
            found.add(path.name)
    return found


def _delegating_aliases(direct: set[str]) -> set[str]:
    """Filenames that ``runpy``-delegate to a *direct* importer.

    ``coordinator-publish.py`` is the live instance: it imports nothing
    publisher-side itself, it re-executes ``publish.py`` via
    ``runpy.run_path``, so it inherits the same unsatisfiable import. One hop
    is enough — the alias convention (``with_name("<target>.py")``) is
    documented in ``coordinator-publish.py``'s own negative-spec as the only
    sanctioned way to add a second name for a bin entrypoint.
    """
    found = set()
    for path in sorted(_BIN_DIR.glob("*.py")):
        if path.name in direct:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "runpy" not in text:
            continue
        for name in re.findall(r'with_name\(\s*["\']([^"\']+)["\']\s*\)', text):
            if name in direct:
                found.add(path.name)
    return found


def _derived_set() -> set[str]:
    direct = _direct_importers()
    return direct | _delegating_aliases(direct)


def test_publisher_only_targets_matches_what_bin_actually_imports():
    """The hand-maintained set equals the set derived from the tree.

    Failing in the "derived has extra" direction means a percolate-
    dispatching CLI shipped without being listed: it resolves to the mirror
    for every non-claude-klabauter session and dies on an import that cannot succeed.
    Failing the other way means a listed target no longer needs the live
    tree (or was renamed) and is now pinned there for no reason.
    """
    derived = _derived_set()
    declared = set(resolve_claude_klabauter.PUBLISHER_ONLY_TARGETS)

    assert derived, (
        "derivation found no percolate-dispatching CLI under "
        f"{_BIN_DIR} — the derivation itself has broken (tokens renamed?), "
        "not the declared set"
    )
    assert derived == declared, (
        "PUBLISHER_ONLY_TARGETS is out of sync with coordinator/bin/.\n"
        f"  missing from the declared set (will resolve to the published "
        f"engine and die on import): {sorted(derived - declared)}\n"
        f"  declared but no longer publisher-only (pinned to the live tree "
        f"for no reason): {sorted(declared - derived)}"
    )


@pytest.mark.parametrize("name", sorted(resolve_claude_klabauter.PUBLISHER_ONLY_TARGETS))
def test_every_declared_target_exists_in_bin(name):
    """A stale entry is inert, and inert entries hide renames."""
    assert (_BIN_DIR / name).is_file(), (
        f"PUBLISHER_ONLY_TARGETS names '{name}' but {_BIN_DIR / name} does not "
        "exist — the target was renamed or removed and the set was not updated"
    )


@pytest.mark.parametrize(
    "spelling",
    ["publish", "publish.py", "coordinator-publish", "coordinator-publish.py"],
)
def test_predicate_accepts_bare_and_suffixed_spellings(spelling):
    """Installed forwarders name one spelling or the other depending on
    install vintage (see ``exec_cli``'s ``.py``-suffix probe). A rule that
    fired for only one would be a coin flip on when the box was installed."""
    assert resolve_claude_klabauter._is_publisher_only_target(spelling) is True


@pytest.mark.parametrize("spelling", ["archive-stamp-cli", "coordinator-doc-new.py"])
def test_predicate_rejects_ordinary_targets(spelling):
    """The carve-out must not widen: everything else keeps the class-aware
    ladder, published-engine divert included."""
    assert resolve_claude_klabauter._is_publisher_only_target(spelling) is False
