"""detect() builds each release state under tmp_path with a bare origin."""

import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workweek_complete.release.state import detect

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

WEEK = "state/week-changelog"
NOTES = "archive/release-notes"


def _g(root, *args, out=False):
    res = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return res.stdout.strip() if out else None


def _write(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _header(root, prior="v0.6.0 (commit abc, 2026-09-06)"):
    _write(root, f"{WEEK}/HEADER.md",
           f"# Week\n\n**Week starting:** 2026-10-05 (ISO W41)\n**Prior week released:** {prior}\n")


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True,
                   **no_console_creationflags())
    _g(work, "init", "-q", "-b", "main")
    _g(work, "remote", "add", "origin", str(origin))
    _write(work, f"{NOTES}/2026-09-06-v0.6.0.md")
    _header(work)
    _g(work, "add", "-A")
    _g(work, "commit", "-qm", "base")
    _g(work, "tag", "v0.6.0")
    _g(work, "push", "-q", "origin", "main")
    _g(work, "fetch", "-q", "origin")
    return work


def _commit(root, msg="c"):
    _g(root, "add", "-A")
    _g(root, "commit", "-qm", msg)


def _state(root, mode="git-tag"):
    return detect(root, tag_prefix="v", release_mode=mode)


def _pending(root):
    _write(root, f"{WEEK}/2026-10-10-pending-release.md")


def _push_fetch(root):
    _g(root, "push", "-q", "origin", "main")
    _g(root, "fetch", "-q", "origin")


def test_empty_week_is_nothing_to_release(repo):
    assert _state(repo).state == "nothing-to-release"


def test_week_with_pending_entry_is_unstamped(repo):
    _pending(repo)
    assert _state(repo).state == "unstamped"


def test_daily_block_is_unstamped(repo):
    _write(repo, f"{WEEK}/2026-10-06-host.md")
    assert _state(repo).state == "unstamped"


def test_stamped(repo):
    _pending(repo)
    _write(repo, f"{NOTES}/2026-10-12-v0.7.0.md")
    _commit(repo)
    s = _state(repo)
    assert (s.state, s.version, s.tag) == ("stamped", "0.7.0", "v0.7.0")


def test_uncommitted_notes_wrapper_reads_unstamped(repo):
    _pending(repo)
    _write(repo, f"{NOTES}/2026-10-12-v0.7.0.md")
    assert _state(repo).state == "unstamped"


def test_placeholder_notes_never_stamped(repo):
    _pending(repo)
    _write(repo, f"{NOTES}/2026-10-12-vX.Y.Z.md")
    _commit(repo)
    assert _state(repo).state == "unstamped"


def test_merged_unpublished(repo):
    _pending(repo)
    _write(repo, f"{NOTES}/2026-10-12-v0.7.0.md")
    _commit(repo, "stamp")
    _push_fetch(repo)
    s = _state(repo)
    assert (s.state, s.version, s.tag) == ("merged-unpublished", "0.7.0", "v0.7.0")
    assert s.merge_sha == _g(repo, "rev-parse", "HEAD", out=True)


def test_published_unarchived_then_archived(repo):
    _pending(repo)
    _write(repo, f"{NOTES}/2026-10-12-v0.7.0.md")
    _commit(repo, "stamp")
    _push_fetch(repo)
    _g(repo, "tag", "v0.7.0")
    s = _state(repo)
    assert s.state == "published-unarchived"
    assert s.merge_sha == _g(repo, "rev-parse", "HEAD", out=True)
    _header(repo, prior="v0.7.0 (commit abc, 2026-10-12)")
    (repo / WEEK / "2026-10-10-pending-release.md").unlink()
    assert _state(repo).state == "nothing-to-release"


def test_week_after_finished_release_is_not_published_unarchived(repo):
    _pending(repo)
    _write(repo, f"{NOTES}/2026-10-12-v0.7.0.md")
    _commit(repo, "stamp")
    _g(repo, "tag", "v0.7.0")
    _header(repo, prior="v0.7.0 (commit abc, 2026-10-12)")
    (repo / WEEK / "2026-10-10-pending-release.md").unlink()
    _commit(repo, "archive")
    _push_fetch(repo)
    assert _state(repo).state == "nothing-to-release"
    _write(repo, f"{WEEK}/2026-10-13-host.md")
    assert _state(repo).state == "unstamped"


def test_release_mode_none_is_unstamped_or_nothing(repo):
    assert _state(repo, "none").state == "nothing-to-release"
    _pending(repo)
    _write(repo, f"{NOTES}/2026-10-12-v0.7.0.md")
    _commit(repo)
    assert _state(repo, "none").state == "unstamped"
