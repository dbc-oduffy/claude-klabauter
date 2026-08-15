"""
coordinator_core.ops.ceremony.tests.test_commit_exec_bit

Tests for the `commit.exec_bit_change` op (commit_exec_bit.py) — the DR-151
Windows-safe exec-bit stage+commit sequence with the B2 foreign-staged-entries
guard (docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B2).

Coverage:
  - DR-151 happy path: mode 100644 → staged chmod → unrestricted commit; HEAD
    records mode 100755 and the returned sha matches HEAD.
  - already-100755 short-circuit: vacuous no-op, nothing committed.
  - CC-4 double invocation: second identical call returns the documented no-op
    shape and HEAD is unchanged.
  - B2 foreign-staged guard (named acceptance criterion): a foreign staged
    entry → structured error NAMING it, no commit lands, and the foreign entry
    is left staged untouched (never swept, never reset/unstaged — DEC-3).
  - target-only staged content is NOT foreign — the commit proceeds.
  - CC-6 dry_run: validation runs, zero writes (index mode still 100644,
    HEAD unchanged), mutation flags false.
  - fail-loud paths: untracked target, missing common_dir, missing path param,
    non-regular-file index mode (CC-7).

All git operations run against a throwaway repo created fresh under
`tmp_path` — never the working repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.ceremony import commit_exec_bit

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_tracked_file(repo: Path, rel_path: str, content: str = "#!/usr/bin/env python3\n") -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", "--", rel_path], repo)
    _git(["commit", "-q", "-m", f"seed {rel_path}"], repo)


def _index_mode(repo: Path, rel_path: str) -> str:
    out = _git(["ls-files", "--stage", "--", rel_path], repo)
    return out.split(None, 1)[0] if out.strip() else ""


def _head_mode(repo: Path, rel_path: str) -> str:
    out = _git(["ls-tree", "HEAD", "--", rel_path], repo)
    return out.split(None, 1)[0] if out.strip() else ""


def _head_sha(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _staged_entries(repo: Path) -> list[str]:
    out = _git(["diff", "--cached", "--name-only"], repo)
    return [ln for ln in out.splitlines() if ln.strip()]


def _call(repo: Path, params: dict) -> dict:
    # Scope common_dir: the handler receives repo_root = the .git directory.
    return commit_exec_bit._handler(params, repo_root=repo / ".git")


# ---------------------------------------------------------------------------
# DR-151 happy path
# ---------------------------------------------------------------------------


def test_promotes_mode_and_commits_unrestricted(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    assert _head_mode(repo, "bin/tool.py") == "100644"
    sha_before = _head_sha(repo)

    result = _call(repo, {"path": "bin/tool.py"})

    assert "error" not in result
    assert result["committed"] is True
    assert result["already_executable"] is False
    assert result["sha"] == _head_sha(repo)
    assert result["sha"] != sha_before
    assert _head_mode(repo, "bin/tool.py") == "100755"
    assert _index_mode(repo, "bin/tool.py") == "100755"


def test_backslash_path_param_normalized(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")

    result = _call(repo, {"path": "bin\\tool.py"})

    assert "error" not in result
    assert result["committed"] is True
    assert _head_mode(repo, "bin/tool.py") == "100755"


# ---------------------------------------------------------------------------
# Idempotency (AC7 / CC-4)
# ---------------------------------------------------------------------------


def test_already_executable_is_vacuous_noop(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    _git(["update-index", "--chmod=+x", "--", "bin/tool.py"], repo)
    _git(["commit", "-q", "-m", "make executable"], repo)
    sha_before = _head_sha(repo)

    result = _call(repo, {"path": "bin/tool.py"})

    assert result == {"committed": False, "sha": None, "already_executable": True}
    assert _head_sha(repo) == sha_before


def test_double_invocation_second_call_is_safe_noop(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")

    first = _call(repo, {"path": "bin/tool.py"})
    assert first["committed"] is True
    sha_after_first = _head_sha(repo)

    second = _call(repo, {"path": "bin/tool.py"})

    assert second == {"committed": False, "sha": None, "already_executable": True}
    assert _head_sha(repo) == sha_after_first
    assert _head_mode(repo, "bin/tool.py") == "100755"


# ---------------------------------------------------------------------------
# B2 foreign-staged-entries guard (named acceptance criterion)
# ---------------------------------------------------------------------------


def test_foreign_staged_entry_fails_loud_naming_it_and_is_left_staged(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    sha_before = _head_sha(repo)

    # A concurrent session's staged work.
    foreign = repo / "docs" / "peer-work.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("a peer session staged this\n", encoding="utf-8")
    _git(["add", "--", "docs/peer-work.md"], repo)

    result = _call(repo, {"path": "bin/tool.py"})

    assert "error" in result
    assert result["committed"] is False
    assert result["sha"] is None
    assert result["foreign_staged"] == ["docs/peer-work.md"]
    assert "docs/peer-work.md" in result["error"]
    # No commit landed.
    assert _head_sha(repo) == sha_before
    # Never swept, never reset/unstaged (DEC-3): the peer's entry is still staged.
    assert _staged_entries(repo) == ["docs/peer-work.md"]
    # The target's mode was never staged either (guard runs before the chmod).
    assert _index_mode(repo, "bin/tool.py") == "100644"


def test_target_only_staged_content_is_not_foreign(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")

    # The target itself has staged content changes — "contains solely the
    # target" per the settlement, so the stage+commit proceeds.
    (repo / "bin" / "tool.py").write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
    _git(["add", "--", "bin/tool.py"], repo)

    result = _call(repo, {"path": "bin/tool.py"})

    assert "error" not in result
    assert result["committed"] is True
    assert _head_mode(repo, "bin/tool.py") == "100755"


# ---------------------------------------------------------------------------
# dry_run (CC-6)
# ---------------------------------------------------------------------------


def test_dry_run_validates_but_writes_nothing(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    sha_before = _head_sha(repo)

    result = _call(repo, {"path": "bin/tool.py", "dry_run": True})

    assert result == {
        "committed": False,
        "sha": None,
        "already_executable": False,
        "dry_run": True,
    }
    assert _head_sha(repo) == sha_before
    assert _index_mode(repo, "bin/tool.py") == "100644"  # chmod never staged
    assert _staged_entries(repo) == []


def test_dry_run_still_fails_loud_on_foreign_staged(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    foreign = repo / "peer.md"
    foreign.write_text("peer\n", encoding="utf-8")
    _git(["add", "--", "peer.md"], repo)

    result = _call(repo, {"path": "bin/tool.py", "dry_run": True})

    assert "error" in result
    assert result["foreign_staged"] == ["peer.md"]


# ---------------------------------------------------------------------------
# Fail-loud paths (CC-7 / validation)
# ---------------------------------------------------------------------------


def test_untracked_path_is_structured_error(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    (repo / "loose.py").write_text("x\n", encoding="utf-8")

    result = _call(repo, {"path": "loose.py"})

    assert "error" in result
    assert "not tracked" in result["error"]
    assert result["committed"] is False


def test_non_regular_file_mode_is_structured_error(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    # Hash a blob and force a symlink-mode (120000) index entry — platform-
    # independent (no real symlink needed; the index entry is what matters).
    blob = _git(["hash-object", "-w", "bin/tool.py"], repo).strip()
    _git(
        ["update-index", "--add", "--cacheinfo", f"120000,{blob},link.py"],
        repo,
    )
    _git(["commit", "-q", "-m", "add symlink entry"], repo)

    result = _call(repo, {"path": "link.py"})

    assert "error" in result
    assert "120000" in result["error"]
    assert result["committed"] is False


def test_missing_common_dir_is_structured_error():
    result = commit_exec_bit._handler({"path": "x.py"}, repo_root=None)
    assert "error" in result
    assert result["committed"] is False


def test_missing_path_param_is_structured_error(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_tracked_file(repo, "bin/tool.py")
    result = _call(repo, {})
    assert "error" in result
    assert "params.path" in result["error"]
