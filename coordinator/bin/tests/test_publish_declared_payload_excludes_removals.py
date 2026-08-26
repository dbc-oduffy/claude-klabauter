"""test_publish_declared_payload_excludes_removals -- pins the disjointness
of `RoundManifest.declared_payload` and `RoundManifest.removed` at the site
that WRITES the manifest (`publish.py :: main`'s per-repo-root manifest
block).

Why this is not a tidiness invariant. The removal side
(`percolate-round.py :: _pathspec_from_manifest`, gated by
`_REMOVAL_SIDE_ENABLED`) derives `(head_tree n row_scope) -
declared_payload`. A path present in BOTH sets is therefore protected from
the deletion the same round explicitly performed -- and the failure is
silent, since the round reports `REMOVE DIR:` and the manifest's own
`removed[]` names the path, so every surface except the derivation says the
removal happened. Measured on the `coordinator-claude` mirror 2026-08-26
(cross-repo/inbox/2026-08-26-doe-claude-em-coordinator-claude-remeasured-
declared-payload-protects-the-removals.md): the retired `whoami/` package sat
in both sets at once, 23 of that mirror's 67 outstanding removals -- the
larger half of what opening the gate was supposed to clear.

Negative-spec: this file does not test `_walk_published_payload`'s prune set
(§ its own callers' tests), does not test `RoundManifest` (de)serialization
(§ `coordinator_core/percolate/tests/test_manifest.py`), and runs no publish
round -- the consequence test builds a `RoundManifest` by hand.

Run: python -m pytest coordinator/bin/tests/test_publish_declared_payload_excludes_removals.py -q
"""
from __future__ import annotations

import ast
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _manifest_block_source() -> str:
    """The `declared_payload` derivation lives inside `publish.py :: main`,
    which cannot be called in a unit test -- so read the statement that builds
    it out of the module's own AST rather than asserting on a substring of the
    whole file (which would pass on a comment mentioning the name)."""
    tree = ast.parse((_BIN_DIR / "publish.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_root_declared_paths" for t in node.targets
        ):
            return ast.unparse(node.value)
    raise AssertionError("publish.py no longer assigns `_root_declared_paths`")


def test_declared_payload_derivation_subtracts_this_run_s_removals():
    """The write site must narrow by `_root_removed` -- the fold of every
    row's `removed_files_sink`, itself sourced from `_report_published_diff`'s
    staging-vs-dest comparison. Widening alone (the surface scan unioned with
    `_walk_published_payload`) re-declares swept paths as payload."""
    derivation = _manifest_block_source()
    assert "- _root_removed" in derivation, (
        "declared_payload must exclude this run's own removals; got: " + derivation
    )


def test_removal_side_cannot_delete_a_path_declared_payload_still_claims():
    """The consequence, stated as a test so the invariant above reads as
    load-bearing rather than decorative: while a swept path stays in
    `declared_payload`, `(head_tree n row_scope) - declared_payload` yields
    nothing for it."""
    from coordinator_core.percolate.manifest import RoundManifest

    head_tree = {"whoami/cli.py", "row_a/foo.txt"}
    row_scope_dirs = {"whoami", "row_a"}

    overlapping = RoundManifest(
        round_id="r1",
        removed=frozenset({"whoami/cli.py"}),
        declared_payload=frozenset({"row_a/foo.txt", "whoami/cli.py"}),
        published_dest_dirs=frozenset(row_scope_dirs),
    )
    disjoint = RoundManifest(
        round_id="r2",
        removed=frozenset({"whoami/cli.py"}),
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset(row_scope_dirs),
    )

    def _removal_set(manifest: RoundManifest) -> set:
        scope = {
            rel
            for rel in head_tree
            if any(rel == d or rel.startswith(d + "/") for d in manifest.published_dest_dirs)
        }
        return scope - manifest.declared_payload

    assert _removal_set(overlapping) == set()
    assert _removal_set(disjoint) == {"whoami/cli.py"}
