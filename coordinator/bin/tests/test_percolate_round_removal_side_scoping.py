"""test_percolate_round_removal_side_scoping -- pins the two-operand fix that
lets the removal side (`_pathspec_from_manifest`, gated by
`_REMOVAL_SIDE_ENABLED`) eventually fire without deleting a live file (§ AC4,
docs/dispatch-briefs/2026-08-26-open-the-percolate-removal-side/C1.md).

`_REMOVAL_SIDE_ENABLED` was `False` when this file was written (AC5) and is
`True` as of `d4ab9fd79` on a PM ruling. The per-test monkeypatch to `True`
therefore now sets what is already the shipped default -- it is kept rather
than dropped so this file still pins the derivation if the flag is ever gated
again, and so no test here depends on the flag's current value. No test here
runs a real percolate round or touches a live publish mirror -- every fixture
is a throwaway `tmp_path` git repo built and torn down within the test.

Negative-spec: this file does not test `RoundManifest`'s own (de)serialization
(§ `coordinator_core/percolate/tests/test_manifest.py` if one exists) and
does not test `_filter_commit_pathspec`'s three safety filters (§
`test_percolate_round_commit_pathspec.py`) -- only the `(head_tree ∩
row_scope) - declared_payload` derivation itself.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_removal_side_scoping.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_removal_side_scoping", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git_run(args, cwd):
    subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True, **_NO_WINDOW)


def _init_repo_with_files(repo_root: Path, files: dict) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git_run(["git", "init", "-q"], repo_root)
    for rel, content in files.items():
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    _git_run(["git", "add", "-A"], repo_root)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "seed"],
        repo_root,
    )


def _no_filter_side_effects(monkeypatch):
    """`_filter_commit_pathspec`'s own probes (`check-ignore`, `ls-files`) hit
    a real `git`, so leave `_run` untouched -- only `_REMOVAL_SIDE_ENABLED`
    is patched by each test below."""
    monkeypatch.setattr(_mod, "_REMOVAL_SIDE_ENABLED", True)


def _symlinks_supported() -> bool:
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as probe_dir:
        target = Path(probe_dir) / "target.txt"
        target.write_text("x")
        link = Path(probe_dir) / "link.txt"
        try:
            os.symlink("target.txt", link)
        except OSError:
            return False
    return True


def test_unprocessed_row_live_files_never_named_for_removal(tmp_path, monkeypatch):
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            "unprocessed_row/legacy.txt": "still here",
        },
    )
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert not any("legacy.txt" in p for p in pathspec)


def test_binary_in_declared_directory_never_named_for_removal(tmp_path, monkeypatch):
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            "row_a/binary.exe": b"\x00\x01binary",
        },
    )
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt", "row_a/binary.exe"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert not any("binary.exe" in p for p in pathspec)


def test_genuinely_stale_path_inside_row_scope_is_named_for_removal(tmp_path, monkeypatch):
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            "row_a/stale.txt": "no longer part of the payload",
        },
    )
    (repo_root / "row_a" / "stale.txt").unlink()
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert any(p.endswith("stale.txt") for p in pathspec)


def test_empty_published_dest_dirs_yields_empty_removal_set(tmp_path, monkeypatch):
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {"row_a/foo.txt": "hello", "row_a/stale.txt": "stale"},
    )
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset(),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert pathspec == []


def test_removal_side_fires_at_the_shipped_flag_value(tmp_path):
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {"row_a/foo.txt": "hello", "row_a/stale.txt": "stale"},
    )
    (repo_root / "row_a" / "stale.txt").unlink()
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert any(p.endswith("stale.txt") for p in pathspec)


@pytest.mark.skipif(
    not _symlinks_supported(), reason="platform cannot create a symlink here (no privilege/Developer Mode)"
)
def test_broken_symlink_is_refused_not_reaped(tmp_path, monkeypatch):
    import os

    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(repo_root, {"row_a/foo.txt": "hello"})
    link = repo_root / "row_a" / "link.txt"
    os.symlink("missing-target.txt", link)
    _git_run(["git", "add", "-A"], repo_root)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "link"],
        repo_root,
    )
    assert not os.path.exists(link) and os.path.lexists(link)

    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    with pytest.raises(_mod.RemovalCandidateOnDiskError):
        _mod._pathspec_from_manifest(manifest, str(repo_root))[0]


@pytest.mark.skipif(
    not _symlinks_supported(), reason="platform cannot create a symlink here (no privilege/Developer Mode)"
)
def test_leg_a_does_not_reap_a_broken_symlink(tmp_path):
    """The SAME `lexists` property on the UNGATED leg -- `manifest.removed`
    needs no `_REMOVAL_SIDE_ENABLED`, so a tracked symlink with a missing
    target is reachable for deletion in a shipped round TODAY, not only once
    the gate opens.

    Leg A skips a path still present in dest's worktree because `git add`
    expresses a deletion only for a path that is GONE; an `exists`-based skip
    follows the link, reads the missing target as "gone", and stages the
    deletion of a symlink that is perfectly present."""
    import os

    repo_root = tmp_path / "repo"
    _init_repo_with_files(repo_root, {"row_a/foo.txt": "hello", "row_b/keep.txt": "k"})
    link = repo_root / "row_b" / "link.txt"
    os.symlink("missing-target.txt", link)
    _git_run(["git", "add", "-A"], repo_root)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "link"],
        repo_root,
    )

    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
        removed=frozenset({"row_b/link.txt"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert not any(p.endswith("link.txt") for p in pathspec)


def test_stranded_root_swap_prior_stands_down_the_removal_side(tmp_path, monkeypatch):
    """AC7: with `_REMOVAL_SIDE_ENABLED` true and a stranded root-swap
    `.prior` sitting at dest root, the removal side must raise BEFORE naming
    any removal -- the strand's subtree is absent from the worktree but still
    tracked at HEAD, which the removal side would otherwise read as retired
    payload (§ P146-C3, `surface.stranded_swap_priors`)."""
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(repo_root, {"row_a/foo.txt": "hello"})
    (repo_root / "docs.prior").mkdir()
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    with pytest.raises(_mod.RemovalCandidateOnDiskError) as excinfo:
        _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert "docs.prior" in str(excinfo.value)


def test_stranded_prior_removed_lets_the_round_proceed_as_before(tmp_path, monkeypatch):
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(repo_root, {"row_a/foo.txt": "hello"})
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert pathspec == []


def test_percolate_bookkeeping_tracked_at_head_is_never_a_removal_candidate(
    tmp_path, monkeypatch
):
    """`.percolate/` is STRUCTURALLY NEVER PUBLISHED, so it must be absent from
    BOTH sides of `row_scope - declared_payload` -- not just from the
    declaration.

    `_walk_published_payload` prunes this prefix set out of `declared_payload`
    on purpose. Before this test, `row_scope` did not prune it, so percolate's
    own `.percolate/round-manifest.json` -- which percolate writes into the
    mirror and which is TRACKED at the mirror's HEAD -- fell out of the
    subtraction as a removal candidate, was found present on disk, and refused
    the round at `_refuse_removals_present_on_disk` before sync.

    Measured on `coordinator-claude` 2026-08-31: every round after a
    fail-closed one died this way, which meant a fail-closed round was not
    self-healing -- it converted a gate refusal into a manifest refusal that
    hid the first one. A flat-mirror row scopes the whole tree, so the bug
    needs no unusual row shape to fire.

    NEGATIVE SPEC: the fix is the prune, NEVER widening `declared_payload` to
    name the file (which the refusal's own remedy text suggests). Declaring it
    would re-admit percolate bookkeeping as publishable payload and, because
    `declared_payload` is SUBTRACTED from `row_scope`, over-declaring silently
    disables the removal side -- the failure the walker's own prune exists to
    prevent.
    """
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            ".percolate/round-manifest.json": '{"round_id": "r0"}',
        },
    )
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"."}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert not any("round-manifest.json" in p for p in pathspec)
    assert not any(".percolate" in p for p in pathspec)


def test_source_retired_path_still_on_disk_is_left_to_leg_a_not_leg_b(
    tmp_path, monkeypatch, capsys
):
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            "whoami/a.md": "a",
            "whoami/b.md": "b",
            "whoami/c.md": "c",
        },
    )
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a", "whoami"}),
        removed=frozenset({"whoami/a.md", "whoami/b.md", "whoami/c.md"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    assert not any("whoami" in p for p in pathspec)
    stderr = capsys.readouterr().err
    assert "3 path(s)" in stderr and "still present in dest's worktree" in stderr


def test_undeclared_on_disk_path_not_in_removed_still_raises(tmp_path, monkeypatch):
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            "whoami/a.md": "a",
            "whoami/b.md": "b",
            "whoami/c.md": "c",
            "whoami/live.md": "still needed",
        },
    )
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a", "whoami"}),
        removed=frozenset({"whoami/a.md", "whoami/b.md", "whoami/c.md"}),
    )
    with pytest.raises(_mod.RemovalCandidateOnDiskError) as excinfo:
        _mod._pathspec_from_manifest(manifest, str(repo_root))
    message = str(excinfo.value)
    assert "live.md" in message
    assert "whoami/a.md" not in message
    assert "whoami/b.md" not in message
    assert "whoami/c.md" not in message
