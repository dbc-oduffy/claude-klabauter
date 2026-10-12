"""publish-evidence rule against a real repo with a local origin/main, and publish_params."""

import hashlib
import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workweek_complete.release.publish import evaluate, publish_params

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

NOTES = "archive/release-notes/v1.md"
BODY = "notes body\n"


def _g(root, *a):
    return subprocess.run(
        ["git", "-C", str(root), *a], check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(root, msg):
    _g(root, "add", "-A")
    _g(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return _g(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _g(tmp_path, "init", "-q")
    (tmp_path / "archive" / "release-notes").mkdir(parents=True)
    (tmp_path / NOTES).write_text(BODY)
    sha = _commit(tmp_path, "release")
    _g(tmp_path, "update-ref", "refs/remotes/origin/main", sha)
    return tmp_path, sha


def _ev(root, sha, **kw):
    args = dict(merge_sha=sha, tag="v1", notes_path=NOTES,
                notes_sha256=hashlib.sha256(BODY.encode()).hexdigest())
    args.update(kw)
    return evaluate(str(root), **args)


def test_passes_with_no_tag(repo):
    root, sha = repo
    r = _ev(root, sha)
    assert r.passed and r.evidence["tag_existed"] is False
    assert r.evidence["merge_sha"] == sha and r.evidence["tag"] == "v1"


def test_merge_not_on_main(repo):
    root, sha = repo
    (root / "x").write_text("x")
    other = _commit(root, "off main")
    r = _ev(root, other)
    assert not r.passed and r.reason == "merge-not-on-main"


def test_tag_on_merge_sha_passes_annotated(repo):
    root, sha = repo
    _g(root, "-c", "user.name=t", "-c", "user.email=t@t", "tag", "-a", "v1", "-m", "m", sha)
    r = _ev(root, sha)
    assert r.passed and r.evidence["tag_existed"] is True


def test_tag_on_other_commit(repo):
    root, sha = repo
    _g(root, "tag", "v1", sha)
    (root / "x").write_text("x")
    newer = _commit(root, "newer")
    _g(root, "update-ref", "refs/remotes/origin/main", newer)
    r = _ev(root, newer)
    assert not r.passed and r.reason == "tag-on-other-commit"


def test_notes_changed(repo):
    root, sha = repo
    r = _ev(root, sha, notes_sha256="0" * 64)
    assert not r.passed and r.reason == "notes-changed"


def test_notes_absent_at_merge_sha(repo):
    root, sha = repo
    r = _ev(root, sha, notes_path="archive/release-notes/missing.md")
    assert not r.passed and r.reason == "notes-changed"


def test_publish_params_gh_release():
    op, p = publish_params("gh-release", repo_root="/r", merge_sha="abc", tag="v1", notes_text="n")
    assert op == "release.cut_tag_and_publish"
    assert p == {"repo_root": "/r", "merge_sha": "abc", "tag_prefix": "v1", "release_notes": "n"}


def test_publish_params_git_tag():
    op, p = publish_params("git-tag", repo_root="/r", merge_sha="abc", tag="v1", notes_text="n")
    assert op == "release.cut_tag"
    assert p == {"repo_root": "/r", "merge_sha": "abc", "tag_prefix": "v1"}


def test_publish_params_none_refused():
    with pytest.raises(ValueError):
        publish_params("none", repo_root="/r", merge_sha="a", tag="v1", notes_text="")
