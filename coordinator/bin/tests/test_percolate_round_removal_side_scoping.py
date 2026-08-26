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
    """`hasattr(os, "symlink")` is True on Windows regardless of privilege --
    the call itself raises `OSError`/`WinError 1314` there without Developer
    Mode or elevation. The only portable check is attempting a real symlink
    and skipping on failure."""
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
    """Witness class 1 (the dispatch brief's measured witness): a row this
    invocation did not process (absent from `published_dest_dirs`) must
    contribute nothing to the removal side, no matter how wide `head_tree`
    is -- `row_scope` excludes it entirely."""
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
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert not any("legacy.txt" in p for p in pathspec)


def test_binary_in_declared_directory_never_named_for_removal(tmp_path, monkeypatch):
    """Witness class 2 (`door.exe`'s shape): a non-transform-eligible payload
    file, tracked at HEAD inside a published dest dir, must be protected by
    the WIDENED `declared_payload` (AC2) -- present here to stand in for what
    publish.py's manifest-write block would have enumerated on disk."""
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
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert not any("binary.exe" in p for p in pathspec)


def test_genuinely_stale_path_inside_row_scope_is_named_for_removal(tmp_path, monkeypatch):
    """Positive control: a path inside the row's OWN published scope, tracked
    at HEAD, genuinely absent from `declared_payload`, AND already gone from
    the worktree -- the class the removal side exists to catch -- must still
    be named.

    The worktree deletion is load-bearing, not fixture noise. This test
    originally left `stale.txt` on disk and asserted it was named anyway;
    `_refuse_removals_present_on_disk` (added by a concurrent session after
    this test landed) correctly refuses that, and the refusal is right for a
    reason measured independently: `explicit_stage` runs `git add -- <paths>`,
    which expresses a deletion only when the path is GONE from the worktree.
    On a path still present and clean it is a pure no-op, so naming one puts
    an entry in the pathspec that silently accomplishes nothing. The premise
    of the old assertion was wrong, not the guard.
    """
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            "row_a/stale.txt": "no longer part of the payload",
        },
    )
    # Committed at HEAD above, then removed from the worktree -- exactly the
    # state a round leaves behind when source stops publishing a path.
    (repo_root / "row_a" / "stale.txt").unlink()
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert any(p.endswith("stale.txt") for p in pathspec)


def test_empty_published_dest_dirs_yields_empty_removal_set(tmp_path, monkeypatch):
    """A manifest with no fourth set (an old manifest on disk, `frozenset()`
    default) must make the removal side fire on NOTHING -- the safe
    fail-direction named in the dispatch brief, exercised even with the gate
    forced on."""
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
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert pathspec == []


def test_removal_side_fires_at_the_shipped_flag_value(tmp_path):
    """The flag is ON (PM, 2026-08-26), so a genuinely stale in-scope path IS
    named with no monkeypatching -- this test previously pinned the opposite
    and is inverted rather than deleted, so the flip is visible in the file's
    own history.

    `stale.txt` is removed from the worktree because that is the only state a
    commit can express a deletion from; a path still on disk is refused by
    `_refuse_removals_present_on_disk`, which its own test covers."""
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
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert any(p.endswith("stale.txt") for p in pathspec)


@pytest.mark.skipif(
    not _symlinks_supported(), reason="platform cannot create a symlink here (no privilege/Developer Mode)"
)
def test_broken_symlink_is_refused_not_reaped(tmp_path, monkeypatch):
    """A TRACKED symlink whose target is missing must be REFUSED by
    `_refuse_removals_present_on_disk`, never named for removal.

    This is the one file class where "not on disk" is a statement about the
    symlink's TARGET rather than about the path itself: `os.path.exists`
    follows the link and reads absent, so an `exists`-based refusal waves the
    candidate through and the removal side deletes a path that is perfectly
    present. `lexists` asks about the link. Zero tracked symlinks sit on
    either mirror today -- this pins the behaviour for the round that
    introduces the first one."""
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
        _mod._pathspec_from_manifest(manifest, str(repo_root))


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

    # `row_b`, deliberately: with the flag ON, a present path inside
    # `row_scope` is refused by the GATED leg before Leg A is reached, and the
    # test would pass for the wrong reason. Leg A iterates `manifest.removed`
    # directly and ignores `row_scope`, so publishing only `row_a` isolates it.
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
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert not any(p.endswith("link.txt") for p in pathspec)
