"""test_percolate_round_commit_pathspec — pins the publish-round commit-
pathspec's safety filtering (docs/plans/2026-08-13-the-publish-round-commits-
the-names-it-a.md § Tasks C3-C5).

REVISED 2026-08-23 (chunk C4, docs/plans/2026-08-23-rebuild-the-percolate-
round-as-six-steps.md AC4/AC5): `_build_commit_pathspec` and
`_split_stdout_by_row_dest` are DELETED -- the commit pathspec is now built
from a `RoundManifest` publish.py's real run persists to disk
(`_read_fresh_round_manifest`/`_pathspec_from_manifest`), never from a
re-parse of that run's stdout. Renames need no resolution machinery under
this shape (a rename is a real-file-presence REMOVE+NEW pair by
construction), so every test below that pinned `_build_commit_pathspec`'s
rename resolution, row attribution, or stdout-derived dedup/containment
logic is REMOVED with it — that mechanism is retired by design, not merely
relocated, and porting a test for code that no longer exists would just
re-describe the deletion.

REVISED AGAIN 2026-08-23 (PM ruling, in-session, "I don't want a dry run, I
never asked for a dry run"): `--dry-run-first` -- the one caller AC5
originally carved `_extract_change_lines` out for -- is ALSO retired
outright, taking `_extract_change_lines` and its block-prefix/rename-tag
parsing tests with it (`_split_stdout_by_row_dest` alone survives, for
`percolate-mirror.py`'s scan-secrets row attribution, unrelated to any of
this). Nothing in this file exercises stdout parsing any more.

What survives, and why: `_filter_commit_pathspec`'s three safety filters
(gitignored-at-dest, already-absent-deletion, repo-root-relative pathing)
are UNCHANGED code -- `_pathspec_from_manifest` reuses it verbatim, only
re-sourcing its `seen` input from the manifest instead of from
`_build_commit_pathspec`. Every test pinning that filtering behaviour is
exercised here by calling `_filter_commit_pathspec` directly via
`_seen_from_change_lines` (a tiny local stand-in for what
`_build_commit_pathspec` used to build internally, minus the rename
resolution and containment check neither of which a manifest-sourced `seen`
dict can ever need: `wire_paths.rel_id` cannot produce a `../`-bearing
entry).

Run: python -m pytest coordinator/bin/tests/test_percolate_round_commit_pathspec.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_commit_pathspec", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _seen_from_change_lines(dest: "str", change_lines) -> dict:
    dest_root = Path(dest)
    seen: dict = {}
    for tag, rel in change_lines:
        seen.setdefault(str(dest_root / rel), (tag, rel))
    return seen


# `_filter_commit_pathspec` itself is UNCHANGED by chunk C4 -- only its


def test_gitignored_path_dropped_from_pathspec(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("NEW", "__pycache__/foo.pyc")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(
                cmd, 0, "__pycache__/foo.pyc\0", ""
            )
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == []


def test_already_absent_deletion_intent_dropped_from_pathspec(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "gone-already.sh")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == []


def test_real_add_update_delete_still_appears_in_pathspec(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("NEW", "added.md"),
        ("UPDATE", "changed.md"),
        ("REMOVE", "still-tracked.sh"),
    ]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "still-tracked.sh\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == [
        str(dest / "added.md"),
        str(dest / "changed.md"),
        str(dest / "still-tracked.sh"),
    ]


def test_filter_summary_printed_to_stderr(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("NEW", "__pycache__/foo.pyc"),
        ("REMOVE", "gone-already.sh"),
        ("NEW", "kept.md"),
    ]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(
                cmd, 0, "__pycache__/foo.pyc\0", ""
            )
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == [str(dest / "kept.md")]
    err = capsys.readouterr().err
    assert "filtered 2 path(s)" in err
    assert "1 gitignored" in err
    assert "1 deletion-intent" in err


def test_pathspec_filter_fails_open_on_undeterminable_dest_state(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "maybe-gone.sh")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == [str(dest / "maybe-gone.sh")]


def test_check_ignore_result_outside_rel_paths_raises_hard(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    seen = {str(dest / "tracked.md"): ("NEW", "tracked.md")}

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "not-in-rel-paths.md\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    with pytest.raises(ValueError, match="check-ignore reported"):
        _mod._filter_commit_pathspec(dest, str(dest), seen)


def test_check_ignore_result_subset_of_rel_paths_does_not_raise(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    seen = {
        str(dest / "ignored.pyc"): ("NEW", "ignored.pyc"),
        str(dest / "kept.md"): ("NEW", "kept.md"),
    }

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "ignored.pyc\0", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    kept = _mod._filter_commit_pathspec(dest, str(dest), seen)[0]
    assert kept == [str(dest / "kept.md")]


import subprocess as _subprocess

_NO_WINDOW = {"creationflags": getattr(_subprocess, "CREATE_NO_WINDOW", 0)}


def _git_run(args, **kwargs):
    return _subprocess.run(args, capture_output=True, text=True, **_NO_WINDOW, **kwargs)


def _init_real_repo(repo_root: Path) -> None:
    _git_run(["git", "init", "-q"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "init"],
        cwd=str(repo_root), check=True,
    )


def test_already_absent_deletion_intent_dropped_from_pathspec_real_repo_subdir(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core"
    dest_subdir.mkdir()
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "second"],
        cwd=str(repo_root), check=True,
    )

    change_lines = [("REMOVE", "ops/ceremony/tests/never_existed.py")]
    pathspec = _mod._filter_commit_pathspec(
        dest_subdir, str(dest_subdir), _seen_from_change_lines(str(dest_subdir), change_lines)
    )[0]
    assert pathspec == []


def test_unstaged_worktree_deletion_kept_but_repo_root_relative(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core"
    tests_dir = dest_subdir / "ops" / "ceremony" / "tests"
    tests_dir.mkdir(parents=True)
    target_file = tests_dir / "test_claim_cli_remedy_invocations.py"
    target_file.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )

    target_file.unlink()

    change_lines = [("REMOVE", "ops/ceremony/tests/test_claim_cli_remedy_invocations.py")]
    pathspec = _mod._filter_commit_pathspec(
        dest_subdir,
        str(dest_subdir),
        _seen_from_change_lines(str(dest_subdir), change_lines),
        repo_root=str(repo_root),
    )[0]
    assert pathspec == ["coordinator_core/ops/ceremony/tests/test_claim_cli_remedy_invocations.py"]

    result = _git_run(["git", "-C", str(repo_root), "ls-files", "--deleted", "--", *pathspec])
    assert result.stdout.strip() == pathspec[0]

    # Pins the actual regression: an ABSOLUTE pathspec entry still scopes
    absolute_form = str(target_file)
    result_absolute = _git_run(
        ["git", "-C", str(repo_root), "ls-files", "--deleted", "--", absolute_form]
    )
    assert result_absolute.stdout.strip() == pathspec[0]
    assert result_absolute.stdout.strip() != absolute_form


def test_repo_root_relative_pathspec_uses_forward_slashes(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core" / "ops"
    tests_dir = dest_subdir / "ceremony" / "tests"
    tests_dir.mkdir(parents=True)
    target_file = tests_dir / "test_nested_deletion.py"
    target_file.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )
    target_file.unlink()

    change_lines = [("REMOVE", "ceremony/tests/test_nested_deletion.py")]
    pathspec = _mod._filter_commit_pathspec(
        dest_subdir,
        str(dest_subdir),
        _seen_from_change_lines(str(dest_subdir), change_lines),
        repo_root=str(repo_root),
    )[0]
    assert pathspec == ["coordinator_core/ops/ceremony/tests/test_nested_deletion.py"]
    assert "\\" not in pathspec[0]


def test_sibling_row_subtree_resolves_without_dotdot(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)

    row_a_dest = repo_root / "coordinator_core"
    row_a_dest.mkdir()
    row_b_dest = repo_root / "coordinator" / "bin"
    row_b_dest.mkdir(parents=True)
    (row_a_dest / "existing.py").write_text("x\n")
    (row_b_dest / "existing-tool").write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )

    row_a_changes = [("NEW", "new-file.py")]
    row_b_changes = [("NEW", "new-tool")]

    pathspec_a = _mod._filter_commit_pathspec(
        row_a_dest,
        str(row_a_dest),
        _seen_from_change_lines(str(row_a_dest), row_a_changes),
        repo_root=str(repo_root),
    )[0]
    pathspec_b = _mod._filter_commit_pathspec(
        row_b_dest,
        str(row_b_dest),
        _seen_from_change_lines(str(row_b_dest), row_b_changes),
        repo_root=str(repo_root),
    )[0]
    combined = pathspec_a + pathspec_b

    assert combined == ["coordinator_core/new-file.py", "coordinator/bin/new-tool"]
    for entry in combined:
        assert ".." not in entry.split("/"), entry
