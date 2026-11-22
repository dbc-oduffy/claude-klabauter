"""A clone whose `remote.origin.pushurl` is on host `blocked.invalid` never publishes, at the sweep and at the push leg."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import push as ceremony_push
from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BRANCH = "work/m/2026-11-22"


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    yield
    push_cadence.reset_cadence_for_test()


@pytest.fixture
def clone(tmp_path):
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    root = tmp_path / "work"
    _git(tmp_path, "init", "-b", "main", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "base")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "checkout", "-b", BRANCH)
    _git(root, "push", "-u", "origin", BRANCH)
    (root / "b.txt").write_text("b")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-m", "fresh commit")
    return root, bare


@pytest.mark.parametrize(
    "pushurl",
    ["blocked.invalid", "https://blocked.invalid/x.git", "git@blocked.invalid:x/y.git", "ssh://u@blocked.invalid:22/x"],
)
def test_sweep_never_publishes_past_a_blocked_pushurl(clone, pushurl):
    root, bare = clone
    before = _git(bare, "rev-parse", BRANCH)
    _git(root, "config", "remote.origin.pushurl", pushurl)
    assert push_cadence._sweep_one(root) == "skipped"
    assert _git(bare, "rev-parse", BRANCH) == before


def test_push_leg_declines_a_blocked_pushurl_without_a_push(clone, monkeypatch):
    root, bare = clone
    before = _git(bare, "rev-parse", BRANCH)
    _git(root, "config", "remote.origin.pushurl", "blocked.invalid")
    monkeypatch.setattr(
        ceremony_push.git_native, "push_streamed",
        lambda *a, **k: pytest.fail("push spawned past a blocked pushurl"),
    )
    outcome = ceremony_push.push_with_retry(root, use_streamed_push=True)
    assert "push:blocked-pushurl" in outcome.skipped
    assert _git(bare, "rev-parse", BRANCH) == before


def test_ordinary_pushurl_is_not_blocked(clone):
    root, bare = clone
    _git(root, "config", "remote.origin.pushurl", str(bare))
    assert push_cadence._sweep_one(root) == "pushed"
    assert _git(bare, "rev-parse", BRANCH) == _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "url,blocked",
    [
        ("blocked.invalid", True),
        ("https://user@BLOCKED.invalid:8443/p", True),
        ("sub.blocked.invalid", True),
        ("notblocked.invalid", False),
        ("https://example.com/blocked.invalid.git", False),
    ],
)
def test_config_scan_matches_host_not_substring(url, blocked):
    text = f'[remote "origin"]\n\turl = x\n\tpushurl = {url}\n'
    assert ceremony_push.config_text_blocks_push(text) is blocked
