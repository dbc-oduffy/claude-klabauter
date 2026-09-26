"""
coordinator_core.ops.tests.test_run_commenting_sweep — characterization tests
for the "ci.run_commenting_sweep" op (coordinator_core.ops.run_commenting_sweep).

No external binary is spawned by the detector itself — `commenting.scan_text`
is a pure regex pass — so these tests run for real against tmp_path-scoped
throwaway git repos; only `git` is invoked as a subprocess.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.ops.run_commenting_sweep  # noqa: F401 -- fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.run_commenting_sweep import (
    _run_commenting_sweep,
    run_commenting_sweep,
    tracked_source_files,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns git as a real subprocess (repo init/add fixtures); runs at cadence
# gates, not per-commit. Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "ci.run_commenting_sweep"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY -- "
    "coordinator_core.ops.run_commenting_sweep @register_op did not fire"
)


def _init_repo(base: Path) -> Path:
    """git-init a throwaway repo with one baseline commit — a default
    branch ref (`main` or `master`, whichever this git chooses) and a
    merge-base only exist once there is at least one commit, and the
    default diff-scope (`changed_files`) has nothing to diff against
    otherwise.
    """
    repo = base / "repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        capture_output=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo,
        capture_output=True,
        **no_console_creationflags(),
    )
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    return repo


def _track_bytes(repo: Path, rel_path: str, content: bytes) -> Path:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(
        ["git", "add", rel_path],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    return path


def _track_file(repo: Path, rel_path: str, content: str) -> Path:
    return _track_bytes(repo, rel_path, content.encode("utf-8"))


def test_op_registered():
    assert _OP_NAME in _REGISTRY


def test_no_tracked_files_returns_empty(tmp_path):
    repo = _init_repo(tmp_path)
    result = run_commenting_sweep(repo)
    assert result == {"findings": [], "files_checked": 0}


def test_changelog_comment_is_reported_invariant_comment_is_clean(tmp_path):
    repo = _init_repo(tmp_path)
    _track_file(
        repo,
        "src/module.py",
        "# fixed the retry loop as of 2026-09-25\n"
        "def f():\n"
        "    # callers assume UTC\n"
        "    return 1\n",
    )
    result = run_commenting_sweep(repo)

    assert result["files_checked"] == 1
    families = {f["family"] for f in result["findings"]}
    assert "changelog_dated" in families
    changelog_finding = next(f for f in result["findings"] if f["family"] == "changelog_dated")
    assert changelog_finding["file"] == "src/module.py"
    assert changelog_finding["line"] == 1
    # the invariant comment on line 3 never fires any family
    assert all(f["line"] != 3 for f in result["findings"])


def test_windows_separator_exempt_path_is_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    # git itself always stores forward-slash paths; the exemption is proven
    # by writing a state/-prefixed path (a real exempt prefix) and asserting
    # it never reaches scan_text, mirroring how a caller-supplied
    # backslash-separated path is normalized by attribution.is_exempt_path
    # before this op ever gets there.
    _track_file(
        repo,
        "state/handoffs/note.py",
        "# fixed the retry loop as of 2026-09-25\n",
    )
    _track_file(repo, "src/clean.py", "# callers assume UTC\n")
    result = run_commenting_sweep(repo)

    assert result["files_checked"] == 1
    assert result["findings"] == []


def test_crlf_input_is_scanned_correctly(tmp_path):
    repo = _init_repo(tmp_path)
    _track_bytes(
        repo,
        "src/windows_style.py",
        b"# fixed the retry loop as of 2026-09-25\r\n"
        b"def f():\r\n"
        b"    # callers assume UTC\r\n"
        b"    return 1\r\n",
    )
    result = run_commenting_sweep(repo)

    assert result["files_checked"] == 1
    assert len(result["findings"]) == 1
    finding = result["findings"][0]
    assert finding["family"] == "changelog_dated"
    assert finding["line"] == 1


def test_json_files_are_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _track_file(repo, "data.json", '{"added by": "someone per the PM"}')
    result = run_commenting_sweep(repo)
    assert result == {"findings": [], "files_checked": 0}


def test_ndjson_and_structural_index_are_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _track_file(repo, "index.ndjson", '{"added by": "someone per the PM"}\n')
    _track_file(
        repo,
        ".structural-index/symbols.ndjson",
        "# fixed the retry loop as of 2026-09-25\n",
    )
    result = run_commenting_sweep(
        repo, paths=["index.ndjson", ".structural-index/symbols.ndjson"]
    )
    assert result == {"findings": [], "files_checked": 0}


def test_default_scope_excludes_unchanged_committed_files(tmp_path):
    """The default (no `paths`) scope only reports what changed against the
    merge-base with the default branch, plus untracked files — a file
    already clean at the baseline commit and never touched again is never
    re-scanned on every later invocation.
    """
    repo = _init_repo(tmp_path)
    _track_file(repo, "src/unchanged.py", "# fixed the retry loop as of 2026-09-25\n")
    subprocess.run(
        ["git", "commit", "-q", "-m", "commit the unchanged file"],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    _track_file(repo, "src/changed.py", "# added by the Game Dev Reviewer, per the PM\n")

    result = run_commenting_sweep(repo)

    files_seen = {f["file"] for f in result["findings"]}
    assert "src/changed.py" in files_seen
    assert "src/unchanged.py" not in files_seen
    assert result["files_checked"] == 1


def test_default_scope_includes_untracked_files(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "untracked.py").write_text(
        "# fixed the retry loop as of 2026-09-25\n", encoding="utf-8"
    )

    result = run_commenting_sweep(repo)

    assert result["files_checked"] == 1
    assert result["findings"][0]["file"] == "untracked.py"


def test_explicit_paths_scans_whole_repo_regardless_of_diff_scope(tmp_path):
    """Passing `paths` is the whole-repo (audit-cadence) form: it scans
    exactly the given list even though nothing in it changed against the
    default-scope baseline.
    """
    repo = _init_repo(tmp_path)
    _track_file(repo, "src/a.py", "# fixed the retry loop as of 2026-09-25\n")
    _track_file(repo, "src/b.py", "# callers assume UTC\n")
    subprocess.run(
        ["git", "commit", "-q", "-m", "commit both"],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )

    default_scope = run_commenting_sweep(repo)
    assert default_scope == {"findings": [], "files_checked": 0}

    whole_repo = run_commenting_sweep(repo, paths=["src/a.py", "src/b.py"])
    assert whole_repo["files_checked"] == 2
    assert whole_repo["findings"][0]["file"] == "src/a.py"

    tracked = tracked_source_files(repo)
    assert set(tracked) >= {"src/a.py", "src/b.py"}


def test_base_param_overrides_default_branch_resolution(tmp_path):
    repo = _init_repo(tmp_path)
    _track_file(repo, "src/older.py", "# added by the Game Dev Reviewer, per the PM\n")
    subprocess.run(
        ["git", "commit", "-q", "-m", "older"],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    older_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    _track_file(repo, "src/newer.py", "# fixed the retry loop as of 2026-09-25\n")
    subprocess.run(
        ["git", "commit", "-q", "-m", "newer"],
        cwd=repo,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )

    default_result = run_commenting_sweep(repo)
    assert default_result == {"findings": [], "files_checked": 0}

    explicit_base_result = run_commenting_sweep(repo, base=older_sha)
    assert explicit_base_result["files_checked"] == 1
    assert explicit_base_result["findings"][0]["file"] == "src/newer.py"


def test_params_repo_root_takes_priority_over_injected(tmp_path):
    repo_a = _init_repo(tmp_path / "a")
    _track_file(repo_a, "one.py", "# clean\n")
    repo_b = _init_repo(tmp_path / "b")
    _track_file(repo_b, "two.py", "# clean\n")
    _track_file(repo_b, "three.py", "# clean\n")

    result = _run_commenting_sweep({"repo_root": str(repo_a)}, repo_root=repo_b)
    assert result["files_checked"] == 1


def test_injected_repo_root_used_when_params_empty(tmp_path):
    repo = _init_repo(tmp_path)
    _track_file(repo, "only.py", "# clean\n")

    result = _run_commenting_sweep({}, repo_root=repo)
    assert result["files_checked"] == 1


def test_double_invocation_is_idempotent(tmp_path):
    repo = _init_repo(tmp_path)
    _track_file(repo, "stable.py", "# fixed the retry loop as of 2026-09-25\n")

    first = run_commenting_sweep(repo)
    second = run_commenting_sweep(repo)
    assert first == second


def test_process_time_is_well_under_the_brightline(tmp_path):
    """claude-klabauter's 200ms single-process bar; measured with process_time, never
    wall clock. Fixture: 1,000 lines of clean, invariant-shaped comments.
    """
    repo = _init_repo(tmp_path)
    lines = "\n".join(f"    # callers assume input {i} is already validated" for i in range(1000))
    _track_file(repo, "src/big.py", lines + "\n")

    start = time.process_time()
    result = run_commenting_sweep(repo)
    elapsed = time.process_time() - start

    assert result["files_checked"] == 1
    assert elapsed < 0.2, f"process_time {elapsed:.4f}s exceeded the 200ms single-process bar"
