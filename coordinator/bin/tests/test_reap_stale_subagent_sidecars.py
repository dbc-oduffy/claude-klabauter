"""test_reap_stale_subagent_sidecars.py — pytest coverage for the WinError
206 fix in reap-stale-subagent-sidecars.py's tracked-reap branch.

Incident: the tracked-reap branch built ONE argv across the WHOLE sidecar
population, twice — `git rm -q -- *tracked_rel` then
`git commit -q -m <msg> -- *tracked_rel`. Windows's `CreateProcess` caps a
command line at ~32767 bytes; the measured live population (6617 tracked
files under `state/subagent-share/`) put 621047 bytes of pathspec argv on
that line — 19x the limit — so `subprocess.run` raised
`FileNotFoundError: [WinError 206] The filename or extension is too long`
on every invocation on this platform. `/workweek-complete` Step 5 calls
this op hand-run, non-zero exit -> surface don't skip, so this failed every
weekly close on Windows.

Fix: `--pathspec-from-file=<f>` (git >= 2.25, supported by both `git rm` and
`git commit` — see `coordinator_core.ops.ceremony.git_native`'s own
`add_paths_pathspec_file`/`commit_with_message_file_pathspec_scoped` for the
same convention already landed elsewhere in this repo) replaces the argv
pathspec list with a constant-size `--pathspec-from-file=<tempfile>` token,
regardless of population size — and keeps the tracked-reap commit atomic
(one commit for the whole population, not one per chunk, which the repo's
own amplification gate
(coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py) would flag
as a per-item git spawn if chunking spawned `git commit` N times instead).

Coverage:
  test_write_pathspec_file_newline_delimited_and_cleanable
  test_tracked_reap_exercises_argv_over_32kb_regime — the actual regression
    test: a synthetic repo with enough tracked sidecars that the OLD argv
    form would have exceeded Windows's 32767-byte CreateProcess ceiling
    (asserted directly against the byte math), reaped via the module's
    real `main()` in a single commit, with the pathspec temp file cleaned
    up afterward.
  test_tracked_reap_small_population_single_commit — small-scale sanity:
    normal reap behavior (removal + one commit) is unchanged by the fix.
  test_dry_run_makes_no_git_rm_or_commit_call — --dry-run still short-circuits
    before any pathspec-file write.

Runs bash-free (spawns real `git` subprocesses against a hermetic tmp repo,
never the shared working tree): `python -m pytest
coordinator/bin/tests/test_reap_stale_subagent_sidecars.py -q`
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

# Spawns real external git processes; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).parent.parent

# Windows CreateProcess command-line ceiling (WinError 206's trigger point).
_WINDOWS_ARGV_CEILING_BYTES = 32767


def _load_module():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(
        "reap_stale_subagent_sidecars", str(_BIN_DIR / "reap-stale-subagent-sidecars.py")
    )
    spec = importlib.util.spec_from_loader("reap_stale_subagent_sidecars", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, **no_console_creationflags()
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture()
def git_repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    # An initial commit so the repo has a HEAD before the reap commit lands.
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-q", "-m", "seed"], tmp_path)
    return tmp_path


def _patch_common(mod, repo_root, *, dead_session_id):
    """Wire main()'s resolver seams so it runs against the hermetic repo
    without touching a real engine root or session-liveness engine."""
    mod.resolve_checked_repo_root = lambda explicit_root=None: (
        str(repo_root),
        {"verdict": "EXPLICIT", "session_root": None, "resolved_root": str(repo_root), "sid": None, "message": ""},
    )
    mod._resolve_session_live = lambda: (lambda session_id, cwd=None: session_id != dead_session_id)


def _write_sidecar(path):
    path.write_text("no frontmatter — status defaults to reapable\n", encoding="utf-8")


# ===========================================================================
# _write_pathspec_file
# ===========================================================================
def test_write_pathspec_file_newline_delimited_and_cleanable():
    mod = _load_module()
    paths = ["state/subagent-share/a/one.md", "state/subagent-share/a/two.md"]
    pathspec_file = mod._write_pathspec_file(paths)
    try:
        assert os.path.isfile(pathspec_file)
        content = Path(pathspec_file).read_text(encoding="utf-8")
        assert content == "state/subagent-share/a/one.md\nstate/subagent-share/a/two.md\n"
    finally:
        os.remove(pathspec_file)
        assert not os.path.exists(pathspec_file)


# ===========================================================================
# The actual regression: the >32KB argv regime.
# ===========================================================================
def test_tracked_reap_exercises_argv_over_32kb_regime(git_repo, monkeypatch):
    mod = _load_module()
    dead_session = "dead-session-id"
    session_dir = git_repo / "state" / "subagent-share" / dead_session
    session_dir.mkdir(parents=True)

    # Enough tracked sidecars that the OLD argv-list `git rm`/`git commit`
    # form would exceed the Windows CreateProcess ceiling. Each relpath
    # runs ~51 bytes; 900 files puts the raw pathspec argv comfortably over
    # 32767 bytes even before accounting for the "git rm -q --" prefix and
    # per-token quoting overhead.
    n_files = 900
    rel_paths = []
    for i in range(n_files):
        p = session_dir / f"sidecar-{i:06d}.md"
        _write_sidecar(p)
        rel_paths.append(os.path.relpath(p, git_repo).replace(os.sep, "/"))

    total_argv_bytes = sum(len(p.encode("utf-8")) + 1 for p in rel_paths)
    assert total_argv_bytes > _WINDOWS_ARGV_CEILING_BYTES, (
        f"test setup must exercise the >32KB regime the bug lived in; got "
        f"{total_argv_bytes} bytes for {n_files} files"
    )

    _git(["add", "-A"], git_repo)
    _git(["commit", "-q", "-m", "seed sidecars"], git_repo)

    _patch_common(mod, git_repo, dead_session_id=dead_session)
    # Tracked-classification is no longer stubbed: it is one `git ls-files`
    # scoped to the reap subtree, not one probe per candidate, so the real
    # call runs here without dominating the runtime of the pathspec-file
    # git rm/commit under test.
    rc = mod.main(["--age-floor-days", "0"])
    assert rc == 0

    # Every sidecar is gone from the working tree...
    # git rm removes the now-empty session directory along with its last
    # tracked file -- absence of the directory itself is as valid a "fully
    # reaped" signal as an empty one.
    assert not session_dir.exists() or not any(session_dir.iterdir())
    # ...and from the index (git rm, not a plain unstaged delete).
    tracked_after = _git(["ls-files", "--", "state/subagent-share"], git_repo)
    assert tracked_after == ""

    # Exactly ONE new commit landed for the whole population (atomicity
    # preserved — the whole point of --pathspec-from-file over chunking).
    log = _git(["log", "--oneline"], git_repo)
    reap_commits = [line for line in log.splitlines() if " reap " in line]
    assert len(reap_commits) == 1, f"expected exactly one reap commit, got:\n{log}"

    reap_commit_files = _git(["show", "--stat", "--format=", "HEAD"], git_repo)
    assert f"{n_files} files changed" in reap_commit_files or str(n_files) in reap_commit_files

    # No leftover pathspec temp file in the OS temp dir from this run.
    import tempfile
    leftovers = [
        f for f in os.listdir(tempfile.gettempdir())
        if f.startswith("reap-stale-sidecars-pathspec-")
    ]
    assert leftovers == [], f"pathspec temp file(s) not cleaned up: {leftovers}"


def test_tracked_reap_small_population_single_commit(git_repo):
    """Sanity: normal-scale reap behavior (removal + one commit) is
    unchanged by the --pathspec-from-file fix."""
    mod = _load_module()
    dead_session = "dead-small"
    session_dir = git_repo / "state" / "subagent-share" / dead_session
    session_dir.mkdir(parents=True)
    for name in ("a.md", "b.md", "c.md"):
        _write_sidecar(session_dir / name)

    _git(["add", "-A"], git_repo)
    _git(["commit", "-q", "-m", "seed small"], git_repo)

    _patch_common(mod, git_repo, dead_session_id=dead_session)

    rc = mod.main(["--age-floor-days", "0"])
    assert rc == 0
    # git rm removes the now-empty session directory along with its last
    # tracked file -- absence of the directory itself is as valid a "fully
    # reaped" signal as an empty one.
    assert not session_dir.exists() or not any(session_dir.iterdir())
    log = _git(["log", "--oneline"], git_repo)
    assert len([line for line in log.splitlines() if " reap " in line]) == 1


def test_dry_run_makes_no_git_rm_or_commit_call(git_repo, monkeypatch):
    mod = _load_module()
    dead_session = "dead-dry"
    session_dir = git_repo / "state" / "subagent-share" / dead_session
    session_dir.mkdir(parents=True)
    _write_sidecar(session_dir / "a.md")

    _git(["add", "-A"], git_repo)
    _git(["commit", "-q", "-m", "seed dry"], git_repo)

    _patch_common(mod, git_repo, dead_session_id=dead_session)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, **kwargs):
        calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", spy_run)

    rc = mod.main(["--dry-run", "--age-floor-days", "0"])
    assert rc == 0
    assert not any(c[:2] == ["git", "rm"] for c in calls if isinstance(c, list))
    assert not any(c[:2] == ["git", "commit"] for c in calls if isinstance(c, list))
    # File untouched.
    assert (session_dir / "a.md").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
