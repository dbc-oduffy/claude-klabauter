"""Tests for coordinator_core.ops.renormalize_index.

Port-parity coverage for coordinator/bin/coordinator-renormalize-index (DOE-PORT
bin-entrypoint variant). Mined from
test-coordinator-renormalize-index.sh (DoE 432e3285, 2026-07-22):

  - Section A (platform-independent safety filters): real edits, worktree deletions,
    and sibling-staged changes are excluded from the phantom set and never staged;
    --check never writes the index; a clean tree is a no-op.
  - Section B (phantom-specific): the EOL phantom-dirty entry itself only manifests on
    Git-for-Windows/NTFS (git there uses a raw stat-size shortcut for `ls-files -m`
    that a `text=auto` clean-filter normalization can desync from the worktree, while
    `git diff` renormalizes and reports equal). On macOS/Linux git resolves the
    size-vs-content difference correctly at the `ls-files -m` layer too, so the fixture
    cannot be manufactured there; the oracle's own test skip-guards this section, and
    this port mirrors that skip-guard rather than asserting a platform behavior that
    does not exist here.
"""
from __future__ import annotations

import contextlib
import io
import subprocess

import pytest

from coordinator_core.ops import renormalize_index as rni
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(cwd, *args, **kwargs):
    # Review: code-reviewer Finding 3 -- caller kwargs win over the helper's
    # own suppression instead of colliding on a shared key (e.g. creationflags).
    run_kwargs = {**no_console_creationflags(), **kwargs}
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        **run_kwargs,
    )


def _new_repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "core.autocrlf", "false")
    (d / ".gitattributes").write_text("* text=auto\n")
    _git(d, "add", ".gitattributes")
    _git(d, "commit", "-qm", "init")
    return d


def _lf(path, body="alpha\nbeta\ngamma\n"):
    path.write_text(body, newline="\n")


def _crlf(path, body="alpha\r\nbeta\r\ngamma\r\n"):
    with open(path, "wb") as fh:
        fh.write(body.encode("ascii"))


def _rev_parse(d, ref):
    res = _git(d, "rev-parse", ref)
    return res.stdout.strip()


def _staged_names(d):
    res = _git(d, "diff", "--cached", "--name-only")
    return set(res.stdout.splitlines())


def _worktree_diff_names(d):
    res = _git(d, "diff", "--name-only")
    return set(res.stdout.splitlines())


def test_not_a_git_repo(tmp_path):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    rc = rni.main([], cwd=str(d))
    assert rc == 1


def test_clean_tree_is_noop(tmp_path, capsys):
    # Review: code-reviewer (Finding 5) -- locks the "stdout is always empty, messages
    # on stderr only" contract; a future print(...) without file=sys.stderr would show
    # up on stdout and this test would catch it.
    d = _new_repo(tmp_path)
    rc = rni.main([], cwd=str(d))
    assert rc == 0
    assert _staged_names(d) == set()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_real_edit_deletion_and_sibling_stage_excluded(tmp_path):
    d = _new_repo(tmp_path)
    _lf(d / "real.txt")
    _lf(d / "gone.txt")
    _lf(d / "sib.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "fixtures")

    (d / "real.txt").write_text("alpha\nREAL-EDIT\ngamma\n", newline="\n")
    (d / "gone.txt").unlink()
    (d / "sib.txt").write_text("alpha\nSIB\ngamma\n", newline="\n")
    _git(d, "add", "sib.txt")

    sib_staged_before = _rev_parse(d, ":sib.txt")

    rc = rni.main([], cwd=str(d))
    assert rc == 0

    staged = _staged_names(d)
    assert "real.txt" not in staged
    assert "gone.txt" not in staged
    assert _rev_parse(d, ":sib.txt") == sib_staged_before
    assert "real.txt" in _worktree_diff_names(d)


def test_weird_named_real_edits_untouched(tmp_path):
    d = _new_repo(tmp_path)
    _lf(d / "with space.txt")
    _lf(d / "uni_cafe.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "weird fixtures")

    (d / "with space.txt").write_text("alpha\nE1\ngamma\n", newline="\n")
    (d / "uni_cafe.txt").write_text("alpha\nE3\ngamma\n", newline="\n")

    rc = rni.main([], cwd=str(d))
    assert rc == 0
    diff_names = _worktree_diff_names(d)
    assert "with space.txt" in diff_names
    assert "uni_cafe.txt" in diff_names
    assert _staged_names(d) == set()


def test_check_only_never_writes_index(tmp_path):
    d = _new_repo(tmp_path)
    _lf(d / "sib.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "fixtures")
    (d / "sib.txt").write_text("alpha\nSIB\ngamma\n", newline="\n")
    _git(d, "add", "sib.txt")

    staged_before = _rev_parse(d, ":sib.txt")
    rc = rni.main(["--check"], cwd=str(d))
    assert rc == 0
    assert _rev_parse(d, ":sib.txt") == staged_before


def test_bulk_real_edits_stay_excluded(tmp_path):
    d = _new_repo(tmp_path)
    for i in range(60):
        _lf(d / f"f{i}.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "bulk")
    for i in range(60):
        (d / f"f{i}.txt").write_text(f"x\n{i}\n", newline="\n")

    rc = rni.main([], cwd=str(d))
    assert rc == 0
    assert _staged_names(d) == set()


def test_index_lock_present_defers(tmp_path):
    d = _new_repo(tmp_path)
    _lf(d / "real.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "fixture")
    (d / "real.txt").write_text("alpha\nEDIT\ngamma\n", newline="\n")

    git_dir = d / ".git"
    lock = git_dir / "index.lock"
    lock.write_text("")
    try:
        rc = rni.main([], cwd=str(d))
        assert rc == 0
        assert _staged_names(d) == set()
    finally:
        lock.unlink()


def test_phantom_dirty_entry_cleared_or_skipped(tmp_path, capsys):
    """Section B (mined from the bash oracle's phantom fixture). Skip-guarded exactly
    as the oracle skip-guards it: the EOL phantom (size-only, content-equal dirty
    status) is a Git-for-Windows/NTFS artifact this platform may not exhibit."""
    d = _new_repo(tmp_path)
    _lf(d / "phantom.txt")
    _lf(d / "realB.txt")
    _lf(d / "sibB.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "fixturesB")

    _crlf(d / "phantom.txt")
    (d / "realB.txt").write_text("alpha\nDELTA\ngamma\n", newline="\n")
    (d / "sibB.txt").write_text("alpha\nSIB\ngamma\n", newline="\n")
    _git(d, "add", "sibB.txt")
    _lf(d / "sibB.txt")

    status = _git(d, "status", "--porcelain", "--", "phantom.txt").stdout.strip()
    if not status:
        pytest.skip("platform does not exhibit EOL phantom-dirty status (not Git-for-Windows)")
    diff = _git(d, "diff", "--quiet", "--", "phantom.txt")
    if diff.returncode != 0:
        pytest.skip("phantom has a real content diff here — fixture not reproducible")

    sib_staged_before = _rev_parse(d, ":sibB.txt")

    rc = rni.main([], cwd=str(d))
    assert rc == 0

    status_after = _git(d, "status", "--porcelain", "--", "phantom.txt").stdout.strip()
    assert status_after == ""
    assert "phantom.txt" not in _staged_names(d)
    assert "realB.txt" in _worktree_diff_names(d)
    assert _rev_parse(d, ":sibB.txt") == sib_staged_before
    # Review: code-reviewer (Finding 5) -- the refreshed-phantom success path is the
    # other case the dispatch brief called out for the stdout-empty contract.
    captured = capsys.readouterr()
    assert captured.out == ""


def test_leading_dash_filename_add_pathspec_safe(tmp_path):
    """Review: code-reviewer (Finding 6) -- `_git_add_pathspec_from_stdin`'s docstring
    documents "safe for leading-dash / space-containing names" (the NUL-safe
    pathspec-from-stdin approach can't be misparsed as a git flag) but no existing test
    exercised a leading-dash name; this directly locks that hazard against the helper
    that actually does the staging."""
    d = _new_repo(tmp_path)
    _lf(d / "-rf.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "dash fixture")
    (d / "-rf.txt").write_text("alpha\nEDIT\ngamma\n", newline="\n")

    ok = rni._git_add_pathspec_from_stdin(["-rf.txt"], cwd=str(d))
    assert ok is True
    assert "-rf.txt" in _staged_names(d)


def test_git_add_refresh_failure_exits_1(tmp_path, monkeypatch):
    """Review: code-reviewer (Finding 3) -- no existing test drove
    `_git_add_pathspec_from_stdin` to return False (the documented `git add
    --ignore-errors` refresh-failure exit-1 branch). A genuine EOL phantom can't be
    manufactured cross-platform (see test_phantom_dirty_entry_cleared_or_skipped), so
    `_git_ls_files_modified` is monkeypatched to force `ghost.txt` (untouched, still
    present in the worktree) into the phantom set, reaching the `git add` staging step
    with a real `git diff`/worktree-existence classification around it."""
    d = _new_repo(tmp_path)
    _lf(d / "ghost.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "fixture")

    monkeypatch.setattr(rni, "_git_ls_files_modified", lambda cwd=None: ["ghost.txt"])
    monkeypatch.setattr(rni, "_git_add_pathspec_from_stdin", lambda paths, cwd=None: False)

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = rni.main([], cwd=str(d))
    err = buf.getvalue()

    assert rc == 1
    assert "WARNING" in err
    assert "git add refresh failed" in err


@pytest.mark.parametrize(
    "helper_name,expected_snippet",
    [
        ("_git_diff_name_only", "git diff"),
        ("_git_ls_files_modified", "git ls-files -m"),
    ],
)
def test_first_probe_failure_exits_1(tmp_path, monkeypatch, helper_name, expected_snippet):
    """Review: code-reviewer (Finding 4) -- no test exercised any of the `git diff`/
    `git ls-files` subprocess-failure exit-1 branches; this covers the two first-pass
    probes (snapshot 1 `git diff` and `git ls-files -m`)."""
    d = _new_repo(tmp_path)

    monkeypatch.setattr(rni, helper_name, lambda cwd=None: None)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = rni.main([], cwd=str(d))
    err = buf.getvalue()

    assert rc == 1
    assert "ERROR" in err
    assert expected_snippet in err


def test_second_diff_probe_failure_exits_1(tmp_path, monkeypatch):
    """Review: code-reviewer (Finding 4) -- covers the third exit-1 branch: the
    re-confirmation `git diff` (snapshot 2) taken immediately before staging."""
    d = _new_repo(tmp_path)
    _lf(d / "phantom.txt")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "fixture")
    # A stat-modified-but-not-content-different entry is not reproducible cross-platform
    # (see test_phantom_dirty_entry_cleared_or_skipped); it suffices here to force at
    # least one non-empty phantom candidate via a monkeypatched ls-files result so
    # execution reaches the snapshot-2 probe, independent of platform EOL behavior.
    monkeypatch.setattr(rni, "_git_ls_files_modified", lambda cwd=None: ["phantom.txt"])

    call_count = {"n": 0}
    real_diff = rni._git_diff_name_only

    def _flaky_diff(cwd=None):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            return None
        return real_diff(cwd)

    monkeypatch.setattr(rni, "_git_diff_name_only", _flaky_diff)

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = rni.main([], cwd=str(d))
    err = buf.getvalue()

    assert rc == 1
    assert "ERROR" in err
    assert "git diff" in err
