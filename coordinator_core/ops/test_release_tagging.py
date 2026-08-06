"""Tests for coordinator_core.ops.release_tagging (ops `release.cut_tag`
and `release.cut_tag_and_publish`, C0a manifest rows
`cut-push-annotated-release-tag` / `cut-push-tag-and-publish-gh-release`).

All git activity runs in tmp_path throwaway repos with a local bare
"origin" (real `git push`/`git tag` exercised); `gh` is never invoked for
real — the module's `_gh` seam is monkeypatched with a stateful fake.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import release_tagging as rt


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path, str]:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(bare))
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")
    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, bare, merge_sha


class _FakeGh:
    """Stateful stand-in for the `gh` CLI: 'releases' is a dict of
    tag -> url; `edit` succeeds only for a tag already in that dict."""

    def __init__(self):
        self.releases: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.create_url = "https://github.com/example/repo/releases/tag/{tag}"

    def __call__(self, args: list[str], cwd=None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ["release", "edit"]:
            tag = args[2]
            if tag in self.releases:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 1, "", "release not found")
        if args[:2] == ["release", "view"]:
            tag = args[2]
            if tag in self.releases:
                return subprocess.CompletedProcess(
                    args, 0, '{"url": "%s"}' % self.releases[tag], ""
                )
            return subprocess.CompletedProcess(args, 1, "", "release not found")
        if args[:2] == ["release", "create"]:
            tag = args[2]
            url = self.create_url.format(tag=tag)
            self.releases[tag] = url
            return subprocess.CompletedProcess(args, 0, url, "")
        raise AssertionError(f"unexpected gh invocation: {args}")


@pytest.fixture()
def repo_env(tmp_path, monkeypatch):
    repo, bare, merge_sha = _init_repo_with_origin(tmp_path)
    fake = _FakeGh()
    monkeypatch.setattr(rt, "_gh", fake)
    return repo, bare, merge_sha, fake


# ---------------------------------------------------------------------------
# release.cut_tag (Mode A)
# ---------------------------------------------------------------------------


def test_cut_tag_creates_and_pushes_annotated_tag(repo_env):
    repo, bare, merge_sha, _fake = repo_env
    out = rt.cut_tag(repo, merge_sha, "v1.0.0")
    assert out == {
        "tag": "v1.0.0",
        "created": True,
        "already_at_sha": False,
        "pushed": True,
    }
    tags = _git(bare, "tag", "--list").stdout
    assert "v1.0.0" in tags
    tag_obj = _git(bare, "cat-file", "-t", "v1.0.0").stdout.strip()
    assert tag_obj == "tag"  # annotated, not lightweight


def test_cut_tag_double_invocation_is_documented_noop(repo_env):
    """AC7: second call with identical inputs is a safe no-op."""
    repo, _bare, merge_sha, _fake = repo_env
    first = rt.cut_tag(repo, merge_sha, "v1.0.0")
    assert first["created"] is True
    second = rt.cut_tag(repo, merge_sha, "v1.0.0")
    assert second == {
        "tag": "v1.0.0",
        "created": False,
        "already_at_sha": True,
        "pushed": False,
    }


def test_cut_tag_prefixed_name_honored_verbatim(repo_env):
    repo, bare, merge_sha, _fake = repo_env
    out = rt.cut_tag(repo, merge_sha, "example-game-repo-v0.4.0")
    assert out["tag"] == "example-game-repo-v0.4.0"
    assert "example-game-repo-v0.4.0" in _git(bare, "tag", "--list").stdout


def test_cut_tag_conflicting_existing_tag_raises(repo_env):
    """A tag already present at a DIFFERENT sha is never force-overwritten."""
    repo, _bare, merge_sha, _fake = repo_env
    (repo / "g.txt").write_text("y")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "second")
    other_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "tag", "-a", "v1.0.0", other_sha, "-m", "v1.0.0")
    with pytest.raises(RuntimeError, match="git tag -a"):
        rt.cut_tag(repo, merge_sha, "v1.0.0")


def test_cut_tag_empty_tag_fails_loud(repo_env):
    repo, _bare, merge_sha, _fake = repo_env
    with pytest.raises(ValueError, match="tag_prefix"):
        rt.cut_tag(repo, merge_sha, "")


def test_cut_tag_empty_merge_sha_fails_loud(repo_env):
    repo, _bare, _merge_sha, _fake = repo_env
    with pytest.raises(ValueError, match="merge_sha"):
        rt.cut_tag(repo, "", "v1.0.0")


def test_cut_tag_leading_dash_tag_rejected(repo_env):
    """Review: code-reviewer (F5, nit) — tag is passed positionally to
    several git subcommands with no `--` separator; a value starting with
    '-' would be misparsed as a git option."""
    repo, _bare, merge_sha, _fake = repo_env
    with pytest.raises(ValueError, match="looks like a git option"):
        rt.cut_tag(repo, merge_sha, "-not-a-tag")


def test_cut_tag_non_repo_root_fails_loud(tmp_path):
    not_a_repo = tmp_path / "notarepo"
    not_a_repo.mkdir()
    with pytest.raises(ValueError, match="not a git worktree"):
        rt.cut_tag(not_a_repo, "deadbeef", "v1.0.0")


def test_cut_tag_handler_registered_and_routes_params(repo_env):
    repo, _bare, merge_sha, _fake = repo_env
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler("release.cut_tag")
    assert handler is not None
    out = asyncio.run(
        handler({"repo_root": str(repo), "merge_sha": merge_sha, "tag_prefix": "v2.0.0"})
    )
    assert out == {
        "tag": "v2.0.0",
        "created": True,
        "already_at_sha": False,
        "pushed": True,
    }


def test_cut_tag_handler_without_repo_root_fails_loud():
    with pytest.raises(ValueError, match="repo_root"):
        asyncio.run(
            rt._cut_tag_handler({"merge_sha": "x", "tag_prefix": "v1.0.0"}, None)
        )


# ---------------------------------------------------------------------------
# release.cut_tag_and_publish (Mode B)
# ---------------------------------------------------------------------------


def test_cut_tag_and_publish_creates_tag_and_release(repo_env):
    repo, bare, merge_sha, fake = repo_env
    out = rt.cut_tag_and_publish(repo, merge_sha, "v1.0.0", "release notes here")
    assert out["tag"] == "v1.0.0"
    assert out["tag_pushed"] is True
    assert out["release_created"] is True
    assert out["release_url"] == fake.create_url.format(tag="v1.0.0")
    assert "v1.0.0" in _git(bare, "tag", "--list").stdout
    # Sequencing: tag-push attempted before the release publish call.
    edit_idx = next(i for i, c in enumerate(fake.calls) if c[:2] == ["release", "edit"])
    assert edit_idx == 0  # edit is attempted first, then falls back to create
    assert fake.calls[1][:2] == ["release", "create"]


def test_cut_tag_and_publish_double_invocation_is_documented_noop(repo_env):
    """AC7: rerun after a full success un-drafts (edits) the existing
    release rather than re-creating it, and does not re-push the tag."""
    repo, _bare, merge_sha, fake = repo_env
    first = rt.cut_tag_and_publish(repo, merge_sha, "v1.0.0", "notes")
    assert first["release_created"] is True

    second = rt.cut_tag_and_publish(repo, merge_sha, "v1.0.0", "notes")
    assert second["tag"] == "v1.0.0"
    assert second["tag_pushed"] is True  # already_at_sha counts as "on origin"
    assert second["release_created"] is False  # edit path, not re-create
    assert second["release_url"] == fake.create_url.format(tag="v1.0.0")


def test_cut_tag_and_publish_tag_push_failure_blocks_release_call(repo_env, monkeypatch):
    """A tag-push failure must surface distinctly and never reach the
    release-publish step (manifest hazard note: sequence tag-push strictly
    before release-publish)."""
    repo, _bare, merge_sha, fake = repo_env

    real_git = rt._git

    def _failing_push(args, cwd=None, timeout=rt._GIT_TIMEOUT):
        if args[:1] == ["push"]:
            return subprocess.CompletedProcess(args, 1, "", "simulated push failure")
        return real_git(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(rt, "_git", _failing_push)
    with pytest.raises(RuntimeError, match="git push"):
        rt.cut_tag_and_publish(repo, merge_sha, "v1.0.0", "notes")
    assert fake.calls == []  # release-publish never attempted


def test_cut_tag_and_publish_handler_registered_and_routes_params(repo_env):
    repo, _bare, merge_sha, fake = repo_env
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler("release.cut_tag_and_publish")
    assert handler is not None
    out = asyncio.run(
        handler(
            {
                "repo_root": str(repo),
                "merge_sha": merge_sha,
                "tag_prefix": "v3.0.0",
                "release_notes": "hello",
            }
        )
    )
    assert out["tag"] == "v3.0.0"
    assert out["release_created"] is True


def test_cut_tag_and_publish_handler_without_repo_root_fails_loud():
    with pytest.raises(ValueError, match="repo_root"):
        asyncio.run(
            rt._cut_tag_and_publish_handler(
                {"merge_sha": "x", "tag_prefix": "v1.0.0", "release_notes": ""}, None
            )
        )
