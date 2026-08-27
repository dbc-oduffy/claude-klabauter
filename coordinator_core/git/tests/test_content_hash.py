"""coordinator_core.git.tests.test_content_hash

Direct unit coverage of `content_matches_index_sha` and the primitives it
composes, now that they live at their C3e home
(`coordinator_core.git.content_hash`) rather than inline in `git_native.py`.
`test_git_native.py`'s own autocrlf-corpus and precondition tests are left
in place and pass unchanged (re-export, see that module's import block) --
this file adds coverage for the one new surface C3e introduces:
`content_matches_index_sha` itself, exercised against a REAL `git add` so
the index sha under test is the one git itself would have written, not a
hand-computed stand-in.

Spec backlink: docs/dispatch-briefs/2026-08-26-the-commit-op-stops-asking-
git-eleven-times/C3e.md
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.git import content_hash


def _real_git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _real_git_out(cwd, *args) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(["init", "-q"], repo)
    _real_git(["config", "user.email", "t@t.example"], repo)
    _real_git(["config", "user.name", "t"], repo)
    return repo


def _index_sha(repo, path) -> str:
    out = _real_git_out(repo, "ls-files", "-s", "--", path)
    # `<mode> <sha> <stage>\t<path>`
    return out.split()[1]


def test_content_matches_index_sha_true_for_a_genuinely_clean_crlf_file(tmp_path):
    """A CRLF file staged, then re-normalized in process, must hash to the
    SAME sha `git add` itself wrote -- the positive case this predicate
    exists to settle without a spawn."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    (repo / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")
    _real_git(["add", "--", "crlf.txt"], repo)
    index_sha = _index_sha(repo, "crlf.txt")

    result = content_hash.content_matches_index_sha(repo, "crlf.txt", index_sha)

    assert result is True


def test_content_matches_index_sha_false_when_worktree_bytes_actually_changed(tmp_path):
    """A staged file whose worktree bytes were edited AFTER staging must
    settle `False`, not `None` -- the divergence caller needs this answer
    to detect the change without a spawn."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    (repo / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")
    _real_git(["add", "--", "crlf.txt"], repo)
    index_sha = _index_sha(repo, "crlf.txt")
    (repo / "crlf.txt").write_bytes(b"line one\r\nline TWO CHANGED\r\n")

    result = content_hash.content_matches_index_sha(repo, "crlf.txt", index_sha)

    assert result is False


def test_content_matches_index_sha_declines_without_autocrlf_true(tmp_path, monkeypatch):
    """No `core.autocrlf=true` resolved anywhere in the layer stack ->
    unconditional decline (`None`), never a guess.

    `GIT_CONFIG_NOSYSTEM` isolates this from the ambient machine config --
    see `test_git_native.py`'s own `test_repo_autocrlf_true_reads_repo_
    local_config` for why: a stock Git for Windows box resolves
    `core.autocrlf=true` from its SYSTEM layer, so a hermetic-looking test
    that does not suppress it would silently assert the wrong thing on
    exactly this box."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(content_hash.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    repo = _init_real_repo(tmp_path)
    (repo / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")
    _real_git(["add", "--", "crlf.txt"], repo)
    index_sha = _index_sha(repo, "crlf.txt")

    result = content_hash.content_matches_index_sha(repo, "crlf.txt", index_sha)

    assert result is None


def test_content_matches_index_sha_declines_on_text_attribute_pin(tmp_path):
    """A repo-local `eol=`/`text` attribute pin is a disposition the C3c
    spike never measured -- decline even though `core.autocrlf=true`."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    (repo / ".gitattributes").write_text("*.sha text eol=lf\n", encoding="utf-8")
    (repo / "digest.sha").write_bytes(b"abc\r\ndef\r\n")
    _real_git(["add", "--", "digest.sha", ".gitattributes"], repo)
    index_sha = _index_sha(repo, "digest.sha")

    result = content_hash.content_matches_index_sha(repo, "digest.sha", index_sha)

    assert result is None


def test_content_matches_index_sha_declines_on_filter_clean_pipeline(tmp_path):
    """A repo-local `filter=` attribute is a different write path entirely
    (a clean driver may transform bytes this module never sees) -- decline
    unconditionally, same as `_hash_worktree_blobs`'s own refusal."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    (repo / "asset.bin").write_bytes(b"abc\r\ndef\r\n")
    _real_git(["add", "--", ".gitattributes"], repo)
    # `asset.bin` is intentionally left OUT of the index -- this predicate
    # is exercised directly, not through `git add` (a real LFS filter isn't
    # installed on this box), so the sha argument is a placeholder; the
    # decline must fire on the attribute alone, before any hash comparison.
    placeholder_sha = "0" * 40

    result = content_hash.content_matches_index_sha(repo, "asset.bin", placeholder_sha)

    assert result is None


def test_content_matches_index_sha_declines_on_unreadable_path(tmp_path):
    """A path absent from the worktree (already deleted) declines rather
    than raising -- the caller's spawn fallback is what reports this."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)

    result = content_hash.content_matches_index_sha(repo, "missing.txt", "0" * 40)

    assert result is None


def test_autocrlf_checkin_normalize_reexported_unchanged_from_git_native(tmp_path):
    """`git_native` re-exports `_autocrlf_checkin_normalize` from this
    module rather than redefining it -- the two must be the SAME object,
    not two independently-maintained copies of the transform."""
    from coordinator_core.ops.ceremony import git_native

    assert git_native._autocrlf_checkin_normalize is content_hash._autocrlf_checkin_normalize
    assert git_native._repo_autocrlf_true is content_hash._repo_autocrlf_true
    assert git_native._text_attribute_pinned is content_hash._text_attribute_pinned
    assert git_native._clean_filter_may_apply is content_hash._clean_filter_may_apply
    assert git_native._system_gitconfig_paths is content_hash._system_gitconfig_paths
