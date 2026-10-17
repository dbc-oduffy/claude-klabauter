"""Call-count pins: attributes and config reads share bytes inside one call_io_scope."""

from __future__ import annotations

import builtins
from collections import Counter
from pathlib import Path

import pytest

from coordinator_core.git import call_io_scope
from coordinator_core.git.checkin_attrs import checkin_disposition
from coordinator_core.git.content_hash import (
    _clean_filter_may_apply,
    _repo_autocrlf_true,
    _text_attribute_pinned,
)

PATHS = [f"sub/deep/file{i}.txt" for i in range(5)] + ["sub/other.md", "top.txt"]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    (root / ".gitattributes").write_text("*.txt text\n*.md -text\n", encoding="utf-8")
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "sub" / ".gitattributes").write_text("*.txt -text\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    return root


def _count_opens(monkeypatch, root: Path) -> Counter:
    opens: Counter = Counter()
    real_open = builtins.open
    prefix = str(root)

    def counting_open(file, *args, **kwargs):
        name = str(file)
        if name.startswith(prefix):
            opens[Path(name).relative_to(root).as_posix()] += 1
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(call_io_scope, "open", counting_open, raising=False)
    return opens


def _answers(root: Path):
    return [
        (
            _clean_filter_may_apply(root, p),
            _text_attribute_pinned(root, p),
            checkin_disposition(root, p),
            _repo_autocrlf_true(root),
        )
        for p in PATHS
    ]


def test_scope_opens_each_file_once(repo, monkeypatch):
    opens = _count_opens(monkeypatch, repo)
    with call_io_scope.call_io_scope():
        _answers(repo)
    assert opens, "no opens observed: the counter is not wired"
    assert max(opens.values()) == 1, dict(opens)
    assert opens[".git/config"] == 1
    assert opens[".gitattributes"] == 1
    assert opens["sub/.gitattributes"] == 1
    assert opens[".git/info/attributes"] == 1


def test_outside_scope_every_read_opens(repo, monkeypatch):
    opens = _count_opens(monkeypatch, repo)
    _answers(repo)
    assert opens[".git/config"] == len(PATHS)
    assert opens[".gitattributes"] > len(PATHS)


def test_answers_identical_inside_and_outside_scope(repo):
    outside = _answers(repo)
    with call_io_scope.call_io_scope():
        inside = _answers(repo)
    assert inside == outside
    assert checkin_disposition(repo, "sub/deep/file0.txt") == "binary"
    assert checkin_disposition(repo, "top.txt") == "text"
    assert checkin_disposition(repo, "sub/other.md") == "binary"
