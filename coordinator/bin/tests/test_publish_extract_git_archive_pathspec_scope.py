"""coordinator/bin/tests/test_publish_extract_git_archive_pathspec_scope.py —
regression pin for `publish.py::_extract_git_archive`'s pathspec-scoped
`git archive` (docs/plans/2026-08-21-the-payload-proves-itself-before-it-
overwrites-the-engine.md C5, `state/dispatch-briefs/2026-08-21-the-payload-
proves-itself-before-it-overwrites-the-engine/C5.md`). Full-tree
materialization of this repo's own HEAD pulled 31,213 files / 413MB to
publish 5,055 files / 86.6MB (~17,667 files of the gap from `state/` alone,
never published by any row) — measured 68.5s -> 11.3s per publish round.

This chunk had NO test coverage before this file. `_required_pathspec_for_
toplevel` is the seam under test: it computes the `git archive` pathspec as
the UNION of every contributing root `setup/publish-targets.portable`
declares and every resolved store-declared inject `src` (§
`_materialize_inject_srcs`) that resolves under the SAME git toplevel —
never re-keying `_MATERIALIZED_REF_CACHE` on the pathspec itself, so the one
shadow tree `run_pre_sync_gates` and `dispatch_percolate_inject` share stays
shared. A naive contributing-roots-only pathspec would silently drop
inject's files from that shared shadow — the exact regression
`test_inject_src_not_covered_by_contributing_roots_alone_is_present` pins,
using the real `cockpit-contract/LICENSE` shape the prior (declined, then
reversed) executor named.

Run: python -m pytest coordinator/bin/tests/test_publish_extract_git_archive_pathspec_scope.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_extract_git_archive_pathspec_scope_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


@pytest.fixture(autouse=True)
def _clear_caches():
    """`_required_pathspec_for_toplevel`/`_git_materialize_ref` are both
    memoized for the process lifetime by design (the exact property C5
    relies on: one shadow tree serving both `run_pre_sync_gates` and
    `dispatch_percolate_inject`). Clear both module-level caches around
    every test so one test's computed pathspec / extracted shadow can never
    leak into the next."""
    publish._REQUIRED_PATHSPEC_CACHE.clear()
    publish._MATERIALIZED_REF_CACHE.clear()
    yield
    publish._REQUIRED_PATHSPEC_CACHE.clear()
    publish._MATERIALIZED_REF_CACHE.clear()


def _resolved_contributing_roots() -> "list[Path]":
    percolate_root, _rung = publish._resolve_percolate_root_and_rung()
    setup_dir = percolate_root / "setup"
    rows = publish.load_targets(setup_dir, target_filter="")
    roots: "list[Path]" = []
    for row in rows:
        target = publish.parse_target_row(row)
        roots.extend(publish._contributing_roots(target))
    return roots


def _resolved_inject_srcs() -> "list[str]":
    percolate_root, _rung = publish._resolve_percolate_root_and_rung()
    setup_dir = percolate_root / "setup"
    return publish._all_inject_srcs_resolved(setup_dir, percolate_root)


# ---------------------------------------------------------------------------
# AC1: the pathspec covers every contributing root AND every resolved
# inject src for the archived toplevel.
# ---------------------------------------------------------------------------


def test_pathspec_covers_every_contributing_root_and_inject_src():
    pathspec = publish._required_pathspec_for_toplevel(publish._REPO_ROOT)
    assert pathspec, "expected a non-empty pathspec for this repo's own toplevel"

    for root in _resolved_contributing_roots():
        if not root.is_dir():
            continue
        root_toplevel = publish._git_rev_parse(root, "--show-toplevel")
        if root_toplevel is None or Path(root_toplevel).resolve() != publish._REPO_ROOT.resolve():
            continue
        entry = publish._relative_pathspec_entry(root, publish._REPO_ROOT)
        assert entry in pathspec, f"contributing root {root} missing from pathspec: {pathspec}"

    for src in _resolved_inject_srcs():
        src_path = Path(src)
        if not src_path.is_absolute() or not src_path.exists():
            continue
        src_toplevel = publish._git_rev_parse(
            src_path if src_path.is_dir() else src_path.parent, "--show-toplevel"
        )
        if src_toplevel is None or Path(src_toplevel).resolve() != publish._REPO_ROOT.resolve():
            continue
        entry = publish._relative_pathspec_entry(src_path, publish._REPO_ROOT)
        assert entry in pathspec, f"inject src {src} missing from pathspec: {pathspec}"


# ---------------------------------------------------------------------------
# AC2: an inject src that would have been dropped by naive
# contributing-roots-only scoping is present in the pathspec — pin the
# regression the prior executor correctly feared, using the
# cockpit-contract/LICENSE shape (a real production inject entry, declared
# in percolate-store.yaml `!`-excluded from its row's own allowlist).
# ---------------------------------------------------------------------------


def test_inject_src_not_covered_by_contributing_roots_alone_is_present():
    pathspec = publish._required_pathspec_for_toplevel(publish._REPO_ROOT)

    contributing_roots = {
        root.resolve()
        for row in publish.load_targets(
            (publish._resolve_percolate_root_and_rung()[0] / "setup"), target_filter=""
        )
        for root in publish._contributing_roots(publish.parse_target_row(row))
    }

    license_src = None
    for src in _resolved_inject_srcs():
        if src.replace("\\", "/").endswith(
            "dist/mirror-native/claude-klabauter/vendor/cockpit-contract/LICENSE"
        ):
            license_src = Path(src)
            break
    assert license_src is not None, "expected the cockpit-contract LICENSE inject entry to resolve"
    assert license_src.exists(), f"resolved inject src does not exist on disk: {license_src}"

    # The regression this pins: LICENSE's containing directory is not a
    # subtree of any contributing root at all — a naive
    # contributing-roots-only pathspec would never include it.
    assert not any(
        root in license_src.resolve().parents or root == license_src.resolve()
        for root in contributing_roots
    ), "test fixture assumption broken: LICENSE now falls under a contributing root"

    entry = publish._relative_pathspec_entry(license_src, publish._REPO_ROOT)
    assert entry in pathspec, (
        f"cockpit-contract/LICENSE inject src {entry!r} missing from pathspec — the exact "
        f"shared-shadow regression C5's dispatch brief names: {pathspec}"
    )


# ---------------------------------------------------------------------------
# AC3: `.percolate-ignore` is named explicitly in the pathspec.
# ---------------------------------------------------------------------------


def test_percolate_ignore_named_explicitly_per_covered_root():
    pathspec = publish._required_pathspec_for_toplevel(publish._REPO_ROOT)
    ignore_entries = [e for e in pathspec if e.endswith(".percolate-ignore")]
    assert ignore_entries, f"expected at least one explicit .percolate-ignore entry: {pathspec}"

    for root in _resolved_contributing_roots():
        ignore_file = root / ".percolate-ignore"
        if not ignore_file.is_file():
            continue
        root_toplevel = publish._git_rev_parse(root, "--show-toplevel")
        if root_toplevel is None or Path(root_toplevel).resolve() != publish._REPO_ROOT.resolve():
            continue
        entry = publish._relative_pathspec_entry(ignore_file, publish._REPO_ROOT)
        assert entry in pathspec, f"{ignore_file} not named explicitly: {pathspec}"


# ---------------------------------------------------------------------------
# AC4: fail-loud on an uncovered required root — an empty pathspec for
# THIS repo's own toplevel raises rather than silently falling back to
# full-tree.
# ---------------------------------------------------------------------------


def test_empty_pathspec_for_own_toplevel_fails_loud(monkeypatch):
    monkeypatch.setattr(publish, "load_targets", lambda *a, **k: [])
    monkeypatch.setattr(publish, "_all_inject_srcs_resolved", lambda *a, **k: [])

    with pytest.raises(publish.GitMaterializeError):
        publish._required_pathspec_for_toplevel(publish._REPO_ROOT)


# ---------------------------------------------------------------------------
# F1 regression (Review: code-reviewer): a contributing root tracked at
# `sha` but absent from the LIVE working tree at pathspec-compute time
# must still appear in the pathspec — the pre-fix code gated coverage on
# `.is_dir()`/`.exists()` against the working tree, not `sha`, and would
# silently drop it.
# ---------------------------------------------------------------------------


def test_root_tracked_at_sha_but_absent_from_working_tree_is_still_covered(tmp_path, monkeypatch):
    # `_required_pathspec_for_toplevel` only scopes `toplevel == _REPO_ROOT`
    # (§ docstring: any other toplevel is "not this pathspec's business").
    # Point `_REPO_ROOT` at a disposable throwaway repo for the duration of
    # this test so the fix is exercised through the same code path
    # `_extract_git_archive` actually uses, without touching this repo's
    # real tree.
    root = tmp_path / "tracked-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "c5-test@claude-klabauter.test"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "C5 Test"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "commit.gpgsign", "false"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    contributing = root / "src"
    contributing.mkdir()
    (contributing / "file.txt").write_bytes(b"hello\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, creationflags=_NO_WINDOW
    )
    sha = publish._git_rev_parse(root, "HEAD")
    assert sha is not None

    import shutil

    shutil.rmtree(contributing)
    assert not contributing.is_dir(), "fixture setup failed: contributing root still on disk"

    monkeypatch.setattr(publish, "_REPO_ROOT", root)
    monkeypatch.setattr(publish, "load_targets", lambda *a, **k: [f"tracked-repo|copy|{contributing}|{tmp_path / 'dest'}"])
    monkeypatch.setattr(publish, "_all_inject_srcs_resolved", lambda *a, **k: [])

    pathspec = publish._required_pathspec_for_toplevel(root, sha)

    entry = publish._relative_pathspec_entry(contributing, root)
    assert entry in pathspec, (
        f"root {contributing} tracked at {sha} but absent from the working tree "
        f"was dropped from the pathspec: {pathspec}"
    )


def test_foreign_toplevel_is_not_scoped_and_does_not_fail_loud(tmp_path):
    """A git toplevel this repo's own configuration declares no coverage
    for (a throwaway repo, e.g. the one `test_publish_git_archive_eol_
    regimes.py` builds directly to exercise `_extract_git_archive` in
    isolation) is explicitly NOT this pathspec's business (dispatch
    brief) — `_required_pathspec_for_toplevel` returns an empty tuple for
    it rather than raising, so `_extract_git_archive` falls through to its
    pre-C5 full-tree behavior for that toplevel unchanged."""
    root = tmp_path / "throwaway-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "c5-test@claude-klabauter.test"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "C5 Test"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "commit.gpgsign", "false"],
        check=True,
        creationflags=_NO_WINDOW,
    )
    (root / "file.txt").write_bytes(b"hello\n")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, creationflags=_NO_WINDOW
    )

    pathspec = publish._required_pathspec_for_toplevel(root)
    assert pathspec == ()


# ---------------------------------------------------------------------------
# Full-tree-vs-scoped measurement (informational, not an AC gate) — the
# dispatch brief asks for real numbers, not estimates.
# ---------------------------------------------------------------------------


def test_scoped_extraction_covers_fewer_files_than_full_tree():
    sha = publish._git_rev_parse(publish._REPO_ROOT, "HEAD")
    assert sha is not None

    shadow = publish._extract_git_archive(publish._REPO_ROOT, sha)
    try:
        scoped_files = sum(1 for _ in shadow.rglob("*") if _.is_file())
        assert scoped_files > 0
        # The measured full-tree count (docs/plans/2026-08-21-the-payload-
        # proves-itself-before-it-overwrites-the-engine.md, this repo's own
        # `git ls-files | wc -l`-adjacent scale) is in the 30k+ range; the
        # scoped extraction must land well under it without pinning an
        # exact, drift-prone count.
        assert scoped_files < 15000, (
            f"scoped extraction pulled {scoped_files} files — expected well under the "
            "31,213-file full-tree baseline"
        )
    finally:
        import shutil

        shutil.rmtree(shadow, ignore_errors=True)
