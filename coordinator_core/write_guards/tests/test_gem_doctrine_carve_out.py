"""Tests for the Group EM doctrine carve-out helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import nomination
from coordinator_core.write_guards import _gem_doctrine_carve_out as co

SID = "sess-gem-1"


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    content = tmp_path / "doe"
    cfg = tmp_path / "claude-home"
    content.mkdir()
    cfg.mkdir()
    co._content_root_memo.clear()
    monkeypatch.setattr(co, "read_content_root", lambda: str(content))
    monkeypatch.setattr(co, "claude_config_dir", lambda: cfg)
    monkeypatch.setattr(
        co._write_bump_applicability, "resolve_launch_anchor", lambda sid, cwd=None, env=None: str(tmp_path / "anchor")
    )
    yield {"content": content, "cfg": cfg}
    co._content_root_memo.clear()


def _identify(state="live", holder_sid=SID, calls=None):
    def fake(repo_root, *a, **k):
        if calls is not None:
            calls.append(repo_root)
        return {"state": state, "holder": {"session_id": holder_sid, "name": "group-em-44"}}

    return fake


LABELED = [
    ("global-doctrine/CLAUDE.md", "global-doctrine"),
    ("coordinator/skills/group-em/SKILL.md", "skills"),
    ("coordinator/commands/x.md", "commands"),
    ("coordinator/agents/x.md", "agents"),
    ("coordinator/hooks/x.py", "hooks"),
    ("coordinator/docs/wiki/coordinator-tripwires/x.md", "wiki"),
    ("coordinator/docs/decisions/DR-x.md", "decisions"),
    ("docs/decisions/DR-x.md", "decisions"),
]


@pytest.mark.parametrize("rel,label", LABELED)
def test_doe_labels_hit(roots, rel, label):
    p = str(roots["content"] / rel)
    assert co.doctrine_surface(p) == label
    assert co.doctrine_root_of(p) == os.path.realpath(roots["content"])


def test_claude_rules_hits_without_content_root(roots):
    p = str(roots["cfg"] / "rules" / "x.md")
    assert co.doctrine_surface(p) == "claude-rules"
    assert co.doctrine_root_of(p) is None


def test_misses(roots, tmp_path):
    misses = [
        roots["cfg"] / "CLAUDE.md",
        roots["content"] / "coordinator/snippets/em-operating-doctrine.md",
        roots["content"] / "coordinator/templates/global-doctrine/CLAUDE.md",
        roots["content"] / "global-doctrine",
        tmp_path / "claude-klabauter/coordinator_core/x.py",
        tmp_path / "klabauter/coordinator/skills/x.md",
    ]
    for m in misses:
        assert co.doctrine_surface(str(m)) is None, m
        assert co.doctrine_root_of(str(m)) is None, m
    assert co.doctrine_surface("") is None


def test_case_and_separator_variants_resolve_the_same(roots):
    base = roots["content"] / "coordinator" / "skills" / "x.md"
    upper = str(base).upper()
    assert co.doctrine_surface(upper) == "skills"
    if os.name == "nt":
        assert co.doctrine_surface(str(base).replace("\\", "/")) == "skills"
        assert co.doctrine_surface(str(base).replace("/", "\\")) == "skills"
        drive = str(base)
        flipped = drive[0].swapcase() + drive[1:]
        assert co.doctrine_surface(flipped) == "skills"


def _payload(**kw):
    return {"session_id": SID, "cwd": "/anywhere", **kw}


def test_live_holder_equal_to_caller_admits(roots, monkeypatch):
    monkeypatch.setattr(nomination, "identify", _identify())
    for rel, _ in LABELED:
        assert co.gem_admitted(_payload(), str(roots["content"] / rel))
    assert co.gem_admitted(_payload(), str(roots["cfg"] / "rules" / "x.md"))


@pytest.mark.parametrize(
    "state,holder",
    [
        ("live", "someone-else"),
        ("ended", SID),
        ("pending", SID),
        ("unverified", SID),
        ("vacant", None),
    ],
)
def test_every_other_standing_refuses(roots, monkeypatch, state, holder):
    monkeypatch.setattr(nomination, "identify", _identify(state, holder))
    assert not co.gem_admitted(_payload(), str(roots["content"] / "coordinator/skills/x.md"))


def test_agent_id_refuses(roots, monkeypatch):
    monkeypatch.setattr(nomination, "identify", _identify())
    assert not co.gem_admitted(_payload(agent_id="sub-1"), str(roots["content"] / "coordinator/skills/x.md"))


def test_claimed_standing_changes_nothing(roots, monkeypatch):
    monkeypatch.setattr(nomination, "identify", _identify("vacant", None))
    monkeypatch.setenv("COORDINATOR_GROUP_EM", "1")
    monkeypatch.setenv("GROUP_EM_SESSION", SID)
    target = str(roots["content"] / "coordinator/skills/x.md")
    assert not co.gem_admitted(_payload(group_em=True, is_group_em=True, gem=SID), target)


def test_path_miss_never_reads_standing(roots, monkeypatch):
    calls: list = []
    monkeypatch.setattr(nomination, "identify", _identify(calls=calls))
    assert not co.gem_admitted(_payload(), str(roots["cfg"] / "CLAUDE.md"))
    assert not co.gem_admitted(_payload(), str(roots["content"] / "coordinator/snippets/x.md"))
    assert calls == []


def test_standing_read_uses_launch_anchor_not_payload_cwd(roots, monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(nomination, "identify", _identify(calls=calls))
    cwd = str(roots["content"])
    assert co.gem_admitted(_payload(cwd=cwd), str(roots["content"] / "coordinator/skills/x.md"))
    assert calls == [str(tmp_path / "anchor")]


def test_no_anchor_refuses(roots, monkeypatch):
    monkeypatch.setattr(nomination, "identify", _identify())
    monkeypatch.setattr(co._write_bump_applicability, "resolve_launch_anchor", lambda *a, **k: None)
    assert not co.gem_admitted(_payload(), str(roots["content"] / "coordinator/skills/x.md"))


def test_live_gem_holder(monkeypatch):
    monkeypatch.setattr(nomination, "identify", _identify("live", "h1"))
    assert co.live_gem_holder("/r") == {"session_id": "h1", "name": "group-em-44"}
    for state in ("ended", "pending", "unverified", "vacant"):
        monkeypatch.setattr(nomination, "identify", _identify(state, "h1"))
        assert co.live_gem_holder("/r") is None
    assert co.live_gem_holder(None) is None

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(nomination, "identify", boom)
    assert co.live_gem_holder("/r") is None


def test_no_subprocess_spawn(roots, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(nomination, "identify", _identify())
    target = str(roots["content"] / "coordinator/skills/x.md")
    assert co.doctrine_surface(target) == "skills"
    assert co.doctrine_root_of(target) is not None
    assert co.gem_admitted(_payload(), target)
    assert co.live_gem_holder("/r") is not None


def test_content_root_unresolvable_leaves_only_rules(roots, monkeypatch):
    co._content_root_memo.clear()
    monkeypatch.setattr(co, "read_content_root", lambda: "")
    assert co.doctrine_surface(str(roots["content"] / "coordinator/skills/x.md")) is None
    assert co.doctrine_surface(str(roots["cfg"] / "rules" / "x.md")) == "claude-rules"
