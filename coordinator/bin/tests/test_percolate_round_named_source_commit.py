"""A percolate round publishes a named committed source ref (default HEAD), never the working tree."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def round_mod():
    return _load("_pr_named_source_round", "percolate-round.py")


@pytest.fixture(scope="module")
def publish_mod():
    return _load("_pr_named_source_publish", "publish.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "mod.py").write_text("committed v1\n", encoding="utf-8")
    _git(root, "add", "mod.py")
    _git(root, "commit", "-q", "-m", "v1")
    first = _git(root, "rev-parse", "HEAD")
    (root / "mod.py").write_text("committed v2\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "v2")
    (root / "mod.py").write_text("DIRTY half-written edit\n", encoding="utf-8")
    (root / "untracked.py").write_text("never committed\n", encoding="utf-8")
    return root, first


def test_default_head_ships_committed_content_not_the_dirty_tree(publish_mod, repo, monkeypatch):
    root, _first = repo
    monkeypatch.setattr(publish_mod, "_source_ref_override", "")
    monkeypatch.setattr(publish_mod, "_MATERIALIZED_REF_CACHE", {})
    pins: dict = {}
    sha = publish_mod._round_pin_source_sha(root, pins)
    shadow = publish_mod._git_materialize_ref(root, ref=sha)

    assert sha == _git(root, "rev-parse", "HEAD")
    assert (shadow / "mod.py").read_text(encoding="utf-8").replace("\r\n", "\n") == "committed v2\n"
    assert not (shadow / "untracked.py").exists()


def test_named_ref_ships_that_commit_and_the_stamp_names_it(publish_mod, round_mod, repo, monkeypatch):
    root, first = repo
    monkeypatch.setattr(publish_mod, "_source_ref_override", first)
    monkeypatch.setattr(publish_mod, "_MATERIALIZED_REF_CACHE", {})
    sha = publish_mod._round_pin_source_sha(root, {})
    shadow = publish_mod._git_materialize_ref(root, ref=sha)

    assert sha == first
    assert (shadow / "mod.py").read_text(encoding="utf-8").replace("\r\n", "\n") == "committed v1\n"
    assert not (shadow / "untracked.py").exists()
    assert round_mod._source_sha_suffix(sha) == f" [source-head {first[:12]}]"


def test_round_cli_accepts_source_ref_and_defaults_to_none(round_mod):
    parser = round_mod._build_parser()
    assert parser.parse_args(["row"]).source_ref is None
    assert parser.parse_args(["row", "--source-ref", "abc123"]).source_ref == "abc123"

