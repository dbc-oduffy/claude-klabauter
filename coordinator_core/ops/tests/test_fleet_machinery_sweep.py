from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops.fleet_machinery_sweep import (
    _PUBLISH_REPO_NAMES,
    _git_ls_files_cached_only,
    _is_publish_repo,
    _relocate_buckets,
    _write_ignore_block,
    discover_sibling_repos,
    dry_run_select,
    select_machinery_paths,
    sweep_repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: str, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        **no_console_creationflags(),
    )


def _write(root: str, rel: str, content: str = "x\n") -> None:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)


def _init_repo(root: str) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")


def test_selector_matches_full_prefix():
    paths = [
        "state/subagent-share/abc/sidecar.md",
        "state/review-trail/findings/x.md",
    ]
    assert select_machinery_paths(paths) == sorted(paths)


def test_selector_rejects_name_token_lookalike():
    paths = [
        "coordinator_core/session/subagent_share.py",
        "docs/reference/state-subagent-share-notes.md",
        "state/subagent-share/real/sidecar.md",
    ]
    selected = select_machinery_paths(paths)
    assert selected == ["state/subagent-share/real/sidecar.md"]
    assert "coordinator_core/session/subagent_share.py" not in selected
    assert "docs/reference/state-subagent-share-notes.md" not in selected


def test_selector_matches_bucket_root_itself():
    paths = ["state/review-trail"]
    assert select_machinery_paths(paths) == ["state/review-trail"]


def test_publish_repo_excluded_by_basename(tmp_path):
    pub = tmp_path / "claude-klabauter"
    pub.mkdir()
    assert _is_publish_repo(str(pub)) is True


def test_repo_with_publish_substring_not_excluded(tmp_path):
    decoy = tmp_path / "claude-klabauter-notes"
    decoy.mkdir()
    assert _is_publish_repo(str(decoy)) is False
    assert "claude-klabauter-notes" not in _PUBLISH_REPO_NAMES


def test_discover_sibling_repos_excludes_self_and_publish(tmp_path):
    self_root = tmp_path / "claude-klabauter"
    sib = tmp_path / "sibling-repo"
    pub = tmp_path / "claude-klabauter"
    non_git = tmp_path / "not-a-repo"
    for d in (self_root, sib, pub, non_git):
        d.mkdir()
    for d in (self_root, sib, pub):
        (d / ".git").mkdir()

    found = discover_sibling_repos(str(self_root))
    found_names = {os.path.basename(p) for p in found}
    assert found_names == {"sibling-repo"}


def test_dry_run_select_full_set(tmp_path):
    root = str(tmp_path)
    _init_repo(root)
    _write(root, "state/subagent-share/abc/sidecar.md")
    _write(root, "coordinator_core/session/subagent_share.py")
    _write(root, "README.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")

    selected = dry_run_select(root)
    assert "state/subagent-share/abc/sidecar.md" in selected
    assert "coordinator_core/session/subagent_share.py" not in selected
    assert "README.md" not in selected


def test_sweep_repo_dry_run_does_not_mutate(tmp_path):
    root = str(tmp_path)
    _init_repo(root)
    _write(root, "state/subagent-share/abc/sidecar.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")

    before_tracked = _git_ls_files_cached_only(root)
    result = sweep_repo(root, mutate=False)
    after_tracked = _git_ls_files_cached_only(root)

    assert before_tracked == after_tracked
    assert not os.path.isdir(os.path.join(root, ".coordinator-local"))
    assert not result["ignore_written"]
    assert "state/subagent-share/abc/sidecar.md" in result["selected"]


def test_write_ignore_block_is_idempotent(tmp_path):
    root = str(tmp_path)
    os.makedirs(root, exist_ok=True)
    first = _write_ignore_block(root)
    second = _write_ignore_block(root)
    assert first is True
    assert second is False
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as fh:
        content = fh.read()
    assert content.count("state/subagent-share/") == 1


def test_sweep_repo_mutate_relocates_and_untracks(tmp_path):
    root = str(tmp_path)
    _init_repo(root)
    _write(root, "state/review-trail/findings/x.md")
    _write(root, "state/subagent-share/abc/sidecar.md")
    _write(root, "README.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")

    result = sweep_repo(root, mutate=True)

    assert result["error"] is None
    assert os.path.isfile(os.path.join(root, ".coordinator-local", "review-trail", "findings", "x.md"))
    assert os.path.isfile(os.path.join(root, ".coordinator-local", "subagent-share", "abc", "sidecar.md"))
    assert not os.path.exists(os.path.join(root, "state", "review-trail"))
    assert not os.path.exists(os.path.join(root, "state", "subagent-share"))
    tracked = set(_git_ls_files_cached_only(root) or [])
    assert "state/review-trail/findings/x.md" not in tracked
    assert "state/subagent-share/abc/sidecar.md" not in tracked
    assert "README.md" in tracked
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True,
        text=True, **no_console_creationflags(),
    )
    assert status.stdout.strip() != ""


def test_relocate_buckets_defers_on_existing_destination(tmp_path):
    root = str(tmp_path)
    _write(root, "state/review-trail/findings/x.md")
    os.makedirs(os.path.join(root, ".coordinator-local", "review-trail"))
    _write(root, ".coordinator-local/review-trail/already-there.md")

    def rename_fn(src, dst):
        os.rename(src, dst)

    moved, deferred = _relocate_buckets(root, rename_fn, buckets=("state/review-trail/",))
    assert moved == []
    assert len(deferred) == 1
    assert deferred[0]["bucket"] == "state/review-trail/"


# subclass), so a narrowing test built on errno 13 asserts the OPPOSITE of

def test_relocate_buckets_records_permanent_oserror_as_deferred(tmp_path):
    root = str(tmp_path)
    _write(root, "state/ceremony/record.md")

    def rename_fn(src, dst):
        raise OSError(9, "Bad file descriptor")

    moved, deferred = _relocate_buckets(root, rename_fn, buckets=("state/ceremony/",))
    assert moved == []
    assert len(deferred) == 1
    assert "Bad file descriptor" in deferred[0]["error"]


def test_errno_13_is_a_permission_error_not_a_bare_oserror():
    exc = OSError(13, "Permission denied")
    assert isinstance(exc, PermissionError)
