"""Guard: a module outside `coordinator_core/git/` that imports one of the
git-object-write / ref-move primitives (`coordinator_core.git.git_objects
.write_object` / `.build_tree` / `.cas_ref` / `.append_reflog`,
`coordinator_core.git.index_write.splice_index`, or
`coordinator_core.git.commit.commit_paths`) reds unless it is named in
`docs/reference/git-action-seam-carve-outs.md`.

Purpose: `docs/plans/2026-08-30-every-deny-capable-guard-fires-on-a-tool-
call-the-op-route-never-makes.md` § C4. The seam this guard protects is
`coordinator_core/git/action_guard.py` (§ C2) -- every Python-side caller of
one of these five primitives is expected to have consulted that seam's
predicates first, or to be a knowing, named exception. A drift guard that
never reds on anything proves the harness runs, not that it discriminates
(this repo's own standing lesson, five instances recorded in one day) --
this module's negative fixture exists to prove the detector actually fires.

DETECTION MECHANISM, NAMED PRECISELY (staff-eng review Finding 7): NOT an
AST scan for "a subprocess spawning git" -- `commit_paths` spawns zero git
processes, so that scan is blind to the very thing this seam guards. NOT an
import-graph scan for `coordinator_core.git.*` generically -- that reds on
the seam's own implementation (`git_objects.py`, `index_write.py`,
`git_dir.py`, `commit.py`, `action_guard.py` itself), none of which is a
violation. The detector is scoped to a SMALL, ENUMERABLE set of import
targets -- see `_GUARDED_TARGETS` below -- checked by `ast.ImportFrom`
against a fixed set of source modules, on files OUTSIDE
`coordinator_core/git/` and outside any `tests/` directory.

Negative-spec:
    - Does NOT flag a bare `subprocess.run(["git", ...])` spawn anywhere --
      out of scope for this list; see
      `docs/reference/git-action-seam-carve-outs.md`'s "Not in scope" section
      for why (covered, if at all, by `test_shared_git_runner.py` and
      `test_no_unbatched_per_item_git_spawn.py`).
    - Does NOT flag a module inside `coordinator_core/git/` -- that
      directory is the seam's own home and is excluded wholesale, not
      carved out entry-by-entry (this is the false-positive surface the
      review found in an unscoped import-graph version of this guard).
    - Does NOT flag a test file (`tests/` directory, or a `test_*.py`
      module) -- a fixture exercising the primitives directly is not a
      production caller.
    - Does NOT accept a carve-out entry merely because it satisfies this
      module's own rationale prose -- membership is ENUMERATION in the
      carve-out doc's machine-readable block, checked by
      `(path, imported_name)`, matching `shell-out-carve-outs.md`'s own
      "enumeration is constitutive, not illustrative" rule.

Spec backlink: docs/plans/2026-08-30-every-deny-capable-guard-fires-on-a-tool-call-the-op-route-never-makes.md, chunk C4
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import FrozenSet, Set, Tuple

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CARVE_OUT_DOC = _REPO_ROOT / "docs/reference/git-action-seam-carve-outs.md"

#: The small, enumerable set of guarded call targets: (source module, name).
#: See this module's own docstring for why these five and not a wider or
#: narrower set.
_GUARDED_TARGETS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("coordinator_core.git.git_objects", "write_object"),
        ("coordinator_core.git.git_objects", "build_tree"),
        ("coordinator_core.git.git_objects", "cas_ref"),
        ("coordinator_core.git.git_objects", "append_reflog"),
        ("coordinator_core.git.index_write", "splice_index"),
        ("coordinator_core.git.commit", "commit_paths"),
    }
)

_GUARDED_NAMES = frozenset(name for _mod, name in _GUARDED_TARGETS)


def _is_excluded_path(rel_posix: str) -> bool:
    """Excluded wholesale: the seam's own home directory, and any test
    file. Neither is a "direct git caller" in the sense this guard polices
    -- see the module docstring's Negative-spec."""
    if rel_posix.startswith("coordinator_core/git/"):
        return True
    if "/tests/" in rel_posix:
        return True
    if rel_posix.rsplit("/", 1)[-1].startswith("test_"):
        return True
    return False


def _guarded_imports_in_source(source: str) -> Set[str]:
    """The subset of `_GUARDED_NAMES` this source file imports from one of
    `_GUARDED_TARGETS`' source modules -- `import ... as x` aliasing is
    resolved to the ORIGINAL name, since the carve-out doc and this
    detector both key on the name being imported, not the local binding."""
    tree = ast.parse(source)
    found: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            if (node.module, alias.name) in _GUARDED_TARGETS:
                found.add(alias.name)
    return found


def _scan_repo_for_direct_callers() -> "list[tuple[str, str]]":
    """`(relpath, imported_name)` for every production, non-seam module
    that imports a guarded target -- the live population this guard's
    parametrized test below checks against the carve-out registry."""
    hits: "list[tuple[str, str]]" = []
    root = _REPO_ROOT / "coordinator_core"
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if _is_excluded_path(rel):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for name in sorted(_guarded_imports_in_source(source)):
            hits.append((rel, name))
    return hits


def _load_carve_out_registry() -> "set[tuple[str, str]]":
    """`(path, imported_name)` pairs the carve-out doc's machine-readable
    block names. A doc that fails to parse, or carries no fenced block, is
    treated as an EMPTY registry -- the closed-list rule (`shell-out-
    carve-outs.md`'s own "enumeration is constitutive") means an unparsed
    doc sanctions nothing, it does not silently sanction everything."""
    text = _CARVE_OUT_DOC.read_text(encoding="utf-8")
    marker = "```yaml git-action-seam-carve-outs"
    start = text.find(marker)
    if start == -1:
        return set()
    start = text.index("\n", start) + 1
    end = text.find("```", start)
    if end == -1:
        return set()
    block = text[start:end]
    entries = yaml.safe_load(block) or []
    registry: "set[tuple[str, str]]" = set()
    for entry in entries:
        path = entry["path"]
        for name in entry.get("imports", []):
            registry.add((path, name))
    return registry


def test_carve_out_doc_exists_and_parses():
    assert _CARVE_OUT_DOC.is_file(), (
        f"{_CARVE_OUT_DOC} is missing -- the guard below has no registry to "
        "check the live repo against"
    )
    registry = _load_carve_out_registry()
    assert registry, (
        "git-action-seam-carve-outs.md's machine-readable block parsed to "
        "zero entries -- either the fenced block is missing/malformed, or "
        "the seed carve-out (coordinator_core/ops/ceremony/git_native.py) "
        "was dropped from it"
    )


def test_every_direct_git_action_caller_is_a_named_carve_out():
    """The live-repo half of the guard: every production, non-seam module
    that imports a guarded call target must appear in the carve-out
    registry. A hit that is NOT registered is exactly the drift this seam
    exists to catch."""
    registry = _load_carve_out_registry()
    live_hits = _scan_repo_for_direct_callers()
    unregistered = [hit for hit in live_hits if hit not in registry]
    assert not unregistered, (
        "direct git-action caller(s) found outside coordinator_core/git/ "
        "with no matching entry in docs/reference/git-action-seam-carve-"
        "outs.md's machine-readable block: "
        f"{unregistered} -- either route the call through "
        "coordinator_core.git.action_guard's predicates, or add a named, "
        "ruled carve-out entry (never both satisfying the rationale and "
        "staying unlisted)."
    )


def test_registered_carve_out_site_actually_exists_and_still_imports_it():
    """The inverse check: a registry entry that no longer matches the repo
    (file moved, import removed) is stale bookkeeping, not evidence the
    guard works -- assert every entry still corresponds to a real import,
    so the registry cannot silently drift from the code it claims to
    describe."""
    registry = _load_carve_out_registry()
    live_hits = set(_scan_repo_for_direct_callers())
    stale = registry - live_hits
    assert not stale, (
        f"carve-out registry entries with no matching live import: {stale} "
        "-- update or remove the stale entry in "
        "docs/reference/git-action-seam-carve-outs.md"
    )


def test_guard_reds_on_an_unregistered_direct_git_action_caller(tmp_path):
    """THE NEGATIVE FIXTURE. An instrument never shown to move proves
    nothing (this repo's own standing lesson) -- this test manufactures a
    synthetic module that imports a guarded target and is deliberately NOT
    in the carve-out registry, and asserts the detector's own predicate
    (the one the two tests above run against the real repo) flags it. This
    is the harness-runs-vs-discriminates distinction: without this test, a
    guard that always passes (e.g. a registry-load bug that treats a
    missing doc as "everything is carved out") would be indistinguishable
    from a working one.
    """
    violating_module = tmp_path / "unregistered_direct_committer.py"
    violating_module.write_text(
        textwrap.dedent(
            """\
            from coordinator_core.git.git_objects import write_object, cas_ref

            def sneaky_commit(gitdir, ref, old, tree_sha, msg):
                commit_sha = write_object(gitdir, b"commit", msg.encode("utf-8"))
                cas_ref(gitdir, ref, old, commit_sha)
                return commit_sha
            """
        ),
        encoding="utf-8",
    )
    rel = "coordinator_core/hooks/unregistered_direct_committer.py"
    assert not _is_excluded_path(rel), (
        "fixture's own relpath must land inside the guard's scan scope, or "
        "this negative fixture proves nothing about the detector"
    )

    source = violating_module.read_text(encoding="utf-8")
    hits = {(rel, name) for name in _guarded_imports_in_source(source)}

    assert hits == {
        (rel, "write_object"),
        (rel, "cas_ref"),
    }, (
        "the unregistered fixture's guarded imports were not detected -- "
        "the detector failed to fire on a call it must catch"
    )

    registry = _load_carve_out_registry()
    unregistered = hits - registry
    assert unregistered, (
        "the negative fixture came back registered -- either the fixture's "
        "relpath collides with a real carve-out entry (test bug) or the "
        "registry check is a no-op (the defect this test exists to catch)"
    )


def test_guard_reds_on_an_unregistered_commit_paths_bypass(tmp_path):
    """THE NEGATIVE FIXTURE for the `commit_paths` bypass this chunk adds.
    `commit_paths` is the op route's own entry point (unlike the five
    lower-level object/ref primitives already covered above) -- a module
    calling it directly, outside the seam and outside the named carve-out
    (`coordinator_core/ops/ceremony/commit_v2.py :: _handler`), is exactly
    the "op route stops being the unguarded default" bypass this plan
    exists to close. Manufactures a synthetic caller and asserts the
    detector fires on it, per this repo's standing lesson that an
    instrument never shown to move proves nothing.
    """
    violating_module = tmp_path / "sneaky_commit_paths_caller.py"
    violating_module.write_text(
        textwrap.dedent(
            """\
            from coordinator_core.git.commit import commit_paths

            def sneaky_commit(repo, paths, message):
                return commit_paths(repo, paths, message)
            """
        ),
        encoding="utf-8",
    )
    rel = "coordinator_core/hooks/sneaky_commit_paths_caller.py"
    assert not _is_excluded_path(rel), (
        "fixture's own relpath must land inside the guard's scan scope, or "
        "this negative fixture proves nothing about the detector"
    )

    source = violating_module.read_text(encoding="utf-8")
    hits = {(rel, name) for name in _guarded_imports_in_source(source)}

    assert hits == {(rel, "commit_paths")}, (
        "the unregistered commit_paths-bypass fixture's guarded import was "
        "not detected -- the detector failed to fire on a call it must "
        "catch"
    )

    registry = _load_carve_out_registry()
    unregistered = hits - registry
    assert unregistered, (
        "the negative fixture came back registered -- either the fixture's "
        "relpath collides with a real carve-out entry (test bug) or the "
        "registry check is a no-op (the defect this test exists to catch)"
    )


def test_no_carve_out_entry_names_a_module_inside_the_seam_itself():
    """Anti-loophole: `coordinator_core/git/` is excluded wholesale (see
    `_is_excluded_path`), so a carve-out entry naming a path inside it
    would be dead bookkeeping riding on a rationale the doc's own "Not in
    scope" section already grants for free -- catches the doc quietly
    growing an entry that satisfies no real gap."""
    registry = _load_carve_out_registry()
    inside_seam = [path for path, _name in registry if path.startswith("coordinator_core/git/")]
    assert not inside_seam, (
        f"carve-out entries naming a path inside coordinator_core/git/ "
        f"itself: {inside_seam} -- that directory is excluded wholesale, "
        "so such an entry sanctions nothing real; remove it"
    )


@pytest.mark.parametrize("mod, name", sorted(_GUARDED_TARGETS))
def test_guarded_target_set_matches_named_primitives(mod, name):
    """Pin the five names in prose against the frozenset in code, so a
    future edit to one without the other is caught here rather than
    silently narrowing or widening what this guard polices."""
    assert (mod, name) in _GUARDED_TARGETS
    assert name in _GUARDED_NAMES
