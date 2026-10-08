"""Tests for collab_detect canonicalization and mailmap parsing."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "collab_detect", Path(__file__).resolve().parents[2] / "lib" / "collab_detect.py"
)
cd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cd)

MM = {"a@work.com": "a@home.com"}


def test_mailmap_unifies_two_spellings():
    assert len(cd.canonicalize(["A@home.com", "a@work.com"], mailmap=MM, bot_excludes=[])) == 1


def test_empty_mailmap_keeps_two():
    assert len(cd.canonicalize(["a@home.com", "a@work.com"], mailmap={}, bot_excludes=[])) == 2


def test_bots_excluded():
    raw = ["x[bot]", "1+u@users.noreply.github.com", "ci@corp.com", "h@x.com"]
    assert cd.canonicalize(raw, mailmap={}, bot_excludes=["CI@corp.com"]) == {"h@x.com"}


def test_alias_mapped_to_bot_excluded():
    assert cd.canonicalize(["o@x.com"], mailmap={"o@x.com": "b@corp.com"}, bot_excludes=["b@corp.com"]) == set()


def test_parse_mailmap_bare_only(tmp_path):
    p = tmp_path / ".mailmap"
    p.write_text(
        "# c\n\n<Canon@X.com> <Alias@X.com>\n"
        "Name <c2@x.com> Other <al2@x.com>\n"
        "Name <c3@x.com>\n",
        encoding="utf-8",
    )
    assert dict(cd.parse_mailmap(p)) == {"alias@x.com": "canon@x.com"}


def test_parse_mailmap_missing_file(tmp_path):
    assert dict(cd.parse_mailmap(tmp_path / "nope")) == {}


@pytest.mark.spawns_process
def _git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.mark.spawns_process
def _commit(repo, email, msg):
    _git(repo, "-c", f"user.email={email}", "-c", "user.name=T", "commit", "--allow-empty", "-m", msg)


def test_parse_git_log_authors_and_trailers():
    out = "\x1ea@x.com\nB <b@x.com>\nC <c@x.com>\n\n\x1ed@x.com\n\n"
    assert cd.parse_git_log(out) == ["a@x.com", "b@x.com", "c@x.com", "d@x.com"]


@pytest.mark.spawns_process
def test_probe_counts_authors_and_coauthor_trailers(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "a@home.com", "one")
    _commit(tmp_path, "a@home.com", "two\n\nCo-authored-by: A Work <A@work.com>\nCo-authored-by: B <b@x.com>")
    (tmp_path / ".mailmap").write_text("<a@home.com> <a@work.com>\n", encoding="utf-8")
    assert cd.count_collaborators(tmp_path) == 2


@pytest.mark.spawns_process
def test_probe_single_commit_with_coauthor_is_multi(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "a@x.com", "solo\n\nCo-authored-by: B <b@x.com>")
    assert cd.count_collaborators(tmp_path) == 2


@pytest.mark.spawns_process
def test_probe_ignores_mailmap_git_side(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "a@work.com", "x")
    (tmp_path / ".mailmap").write_text("<a@home.com> <a@work.com>\n", encoding="utf-8")
    assert cd.probe_raw_emails(tmp_path) == ["a@work.com"]


@pytest.mark.spawns_process
def test_probe_non_repo_and_unborn_head(tmp_path):
    assert cd.probe_raw_emails(tmp_path) == []
    _git(tmp_path, "init", "-q")
    assert cd.probe_raw_emails(tmp_path) == []


def test_canonicalize_does_not_touch_disk(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("disk read")
    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr("builtins.open", boom)
    assert cd.canonicalize(["a@x.com"], mailmap={}, bot_excludes=[]) == {"a@x.com"}
