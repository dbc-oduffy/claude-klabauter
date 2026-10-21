"""memo.migrate_group_em_inbox moves group-em home inbox memos to the G-EM reroute target.

Receivers are real tmp_path git repos; the resolver and home are pinned per test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import gem_home
from coordinator_core.ops.fleet import memo_migrate_group_em_inbox as mod
from coordinator_core.ops.fleet.tests.test_memo_send import _git, _make_receiver_git_repo
from coordinator_core.ops.fleet.memo_migrate_group_em_inbox import (
    _memo_migrate_group_em_inbox,
    migrate_group_em_inbox,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BODY = "---\nfrom: x-em\nto: group-em-em\n---\nbody {n}\n"


def _out(cp) -> str:
    out = cp.stdout
    return (out.decode() if isinstance(out, bytes) else out).strip()


def _home(tmp_path: Path, names_current=("a.md", "b.md"), names_legacy=("c.md",)) -> Path:
    home = tmp_path / "gem-home"
    for names, layout in ((names_current, ("state", "cross-repo", "inbox")), (names_legacy, ("cross-repo", "inbox"))):
        d = home.joinpath(*layout)
        d.mkdir(parents=True)
        for i, n in enumerate(names):
            (d / n).write_text(_BODY.format(n=n), encoding="utf-8", newline="\n")
    return home


@pytest.fixture
def env(tmp_path, monkeypatch):
    receiver = _make_receiver_git_repo(tmp_path)
    home = _home(tmp_path)
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(
        mod, "resolve_receiver_inbox",
        lambda rid: (receiver / "cross-repo" / "inbox", receiver, {}),
    )
    return home, receiver


def _receiver_commit_count(receiver: Path) -> int:
    return int(_out(_git(receiver, "rev-list", "--count", "HEAD")))


def test_three_memos_land_committed_and_home_inbox_empties(env):
    home, receiver = env
    before = _receiver_commit_count(receiver)

    result = migrate_group_em_inbox()

    assert result["exit_code"] == 0, result
    assert sorted(a["id"] for a in result["acted"]) == ["a.md", "b.md", "c.md"]
    assert {a["action"] for a in result["acted"]} == {mod.ACTION_MOVED}
    for n in ("a.md", "b.md", "c.md"):
        assert (receiver / "cross-repo" / "inbox" / n).read_text(encoding="utf-8") == _BODY.format(n=n)
    assert _receiver_commit_count(receiver) == before + 3
    assert _out(_git(receiver, "status", "--porcelain")) == ""
    assert not list(home.rglob("*.md"))


def test_rerun_is_a_noop(env):
    migrate_group_em_inbox()
    again = migrate_group_em_inbox()
    assert again["exit_code"] == 0
    assert again["acted"] == [] and again["failed"] == []


def test_identical_bytes_at_target_removes_home_copy_only(env):
    home, receiver = env
    migrate_group_em_inbox()
    (home / "state" / "cross-repo" / "inbox" / "a.md").write_text(_BODY.format(n="a.md"), encoding="utf-8", newline="\n")
    before = _receiver_commit_count(receiver)

    result = migrate_group_em_inbox()

    assert [(a["id"], a["action"]) for a in result["acted"]] == [("a.md", mod.ACTION_ALREADY_PRESENT)]
    assert _receiver_commit_count(receiver) == before
    assert not (home / "state" / "cross-repo" / "inbox" / "a.md").exists()


def test_identical_uncommitted_target_is_pending_and_home_copy_stays(env):
    home, receiver = env
    target = receiver / "cross-repo" / "inbox" / "a.md"
    target.write_text(_BODY.format(n="a.md"), encoding="utf-8", newline="\n")
    src = home / "state" / "cross-repo" / "inbox" / "a.md"
    before = _receiver_commit_count(receiver)

    result = migrate_group_em_inbox()

    pending = [f for f in result["failed"] if f["reason"].startswith(mod.REASON_PENDING)]
    assert [f["id"] for f in pending] == ["a.md"]
    assert "a.md" not in [a["id"] for a in result["acted"]]
    assert src.exists()
    assert _receiver_commit_count(receiver) == before + 2


def test_differing_bytes_is_a_collision_and_both_stay(env):
    home, receiver = env
    target = receiver / "cross-repo" / "inbox" / "a.md"
    target.write_text("different\n", encoding="utf-8", newline="\n")
    _git(receiver, "add", "-A")
    _git(receiver, "commit", "-m", "seed a")
    src = home / "state" / "cross-repo" / "inbox" / "a.md"
    src_bytes = src.read_bytes()

    result = migrate_group_em_inbox()

    collisions = [f for f in result["failed"] if f["reason"].startswith(mod.REASON_COLLISION)]
    assert [f["id"] for f in collisions] == ["a.md"]
    assert result["exit_code"] == 2
    assert src.read_bytes() == src_bytes
    assert target.read_text(encoding="utf-8") == "different\n"
    assert sorted(a["id"] for a in result["acted"]) == ["b.md", "c.md"]


def test_refused_target_moves_nothing(tmp_path, monkeypatch):
    home = _home(tmp_path)
    not_a_checkout = tmp_path / "plain-dir"
    (not_a_checkout / "state" / "cross-repo" / "inbox").mkdir(parents=True)
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(
        mod, "resolve_receiver_inbox",
        lambda rid: (not_a_checkout / "state" / "cross-repo" / "inbox", not_a_checkout, {}),
    )

    result = migrate_group_em_inbox()

    assert result["acted"] == []
    assert sorted(f["id"] for f in result["failed"]) == ["a.md", "b.md", "c.md"]
    assert all(f["reason"].startswith(mod.REASON_REFUSED) for f in result["failed"])
    assert len(list(home.rglob("*.md"))) == 3
    assert not list((not_a_checkout / "state" / "cross-repo" / "inbox").glob("*.md"))


def test_unresolvable_receiver_moves_nothing(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(mod, "resolve_receiver_inbox", lambda rid: (None, None, {}))

    result = migrate_group_em_inbox()

    assert result["acted"] == [] and len(result["failed"]) == 3
    assert len(list(home.rglob("*.md"))) == 3


def test_dry_run_mutates_nothing(env):
    home, receiver = env
    before = _receiver_commit_count(receiver)

    result = _memo_migrate_group_em_inbox({"dry_run": True})

    assert result["dry_run"] is True
    assert sorted(c["id"] for c in result["candidates"]) == ["a.md", "b.md", "c.md"]
    assert _receiver_commit_count(receiver) == before
    assert len(list(home.rglob("*.md"))) == 3


def test_refused_home_is_a_setup_error(tmp_path, monkeypatch):
    def _refuse():
        raise gem_home.HomeRefused("h", "c")

    monkeypatch.setattr(gem_home, "resolve_home", _refuse)
    result = migrate_group_em_inbox()
    assert result["exit_code"] == 1


def test_unknown_param_is_refused():
    assert _memo_migrate_group_em_inbox({"dry_run": False, "x": 1})["exit_code"] == 1
    assert _memo_migrate_group_em_inbox({"dry_run": "no"})["exit_code"] == 1


def test_empty_or_absent_home_inbox_spawns_nothing(tmp_path, monkeypatch):
    home = tmp_path / "empty-home"
    (home / "state" / "cross-repo" / "inbox").mkdir(parents=True)
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert migrate_group_em_inbox()["acted"] == []

    monkeypatch.setattr(gem_home, "resolve_home", lambda: tmp_path / "no-such-home")
    assert migrate_group_em_inbox()["acted"] == []


def test_delivery_path_spawns_no_hook_bearing_git(env, monkeypatch):
    seen = []
    real = subprocess.Popen

    def _spy(args, *a, **k):
        seen.append(args)
        return real(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", _spy)
    result = migrate_group_em_inbox()
    assert result["exit_code"] == 0
    flat = [" ".join(map(str, s)) if isinstance(s, (list, tuple)) else str(s) for s in seen]
    assert not [s for s in flat if " commit " in f" {s} "], flat


def test_tracked_home_copy_removal_is_committed(env):
    home, _receiver = env
    (home / "CLAUDE.md").write_text("home\n", encoding="utf-8")
    _git(home, "init", "-b", "main")
    _git(home, "config", "user.email", "test@claude-klabauter.test")
    _git(home, "config", "user.name", "ClaudeKlabauterTest")
    _git(home, "config", "commit.gpgsign", "false")
    _git(home, "add", "-A")
    _git(home, "commit", "-m", "seed")

    result = migrate_group_em_inbox()

    assert result["exit_code"] == 0, result["failed"]
    assert [p.name for p in home.rglob("*.md")] == ["CLAUDE.md"]
    assert _out(_git(home, "status", "--porcelain")) == ""
    assert _out(_git(home, "ls-files")) == "CLAUDE.md"
