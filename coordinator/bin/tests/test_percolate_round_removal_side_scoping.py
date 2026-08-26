"""test_percolate_round_removal_side_scoping -- pins the two-operand fix that
lets the removal side (`_pathspec_from_manifest`, gated by
`_REMOVAL_SIDE_ENABLED`) eventually fire without deleting a live file (§ AC4,
docs/dispatch-briefs/2026-08-26-open-the-percolate-removal-side-without-
65ff4e/C1.md).

`_REMOVAL_SIDE_ENABLED` stays `False` in `percolate-round.py` itself (AC5) --
this file monkeypatches it `True` per-test to exercise the derivation the
flag currently gates, never to flip the shipped default. No test here runs a
real percolate round or touches a live publish mirror -- every fixture is a
throwaway `tmp_path` git repo built and torn down within the test.

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
    at HEAD, and genuinely absent from `declared_payload` -- the class the
    removal side exists to catch -- must still be named."""
    _no_filter_side_effects(monkeypatch)
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {
            "row_a/foo.txt": "hello",
            "row_a/stale.txt": "no longer part of the payload",
        },
    )
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


def test_removal_side_dormant_while_flag_is_false(tmp_path):
    """Regression pin for AC5: with `_REMOVAL_SIDE_ENABLED` at its shipped
    `False` value, a genuinely stale in-scope path is NOT named -- the gate
    the flag exists to hold shut."""
    repo_root = tmp_path / "repo"
    _init_repo_with_files(
        repo_root,
        {"row_a/foo.txt": "hello", "row_a/stale.txt": "stale"},
    )
    manifest = _mod._RoundManifest(
        round_id="r1",
        declared_payload=frozenset({"row_a/foo.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    pathspec = _mod._pathspec_from_manifest(manifest, str(repo_root))
    assert not any(p.endswith("stale.txt") for p in pathspec)
