"""Pins the report interface of `commit_leg_profile` that C2, C6 and C7 read.

The builder runs against a tiny tmp_path repo, never a clone of this repo.
"""
import subprocess

import pytest

from coordinator_core.benchmarks import commit_leg_profile as clp

pytestmark = pytest.mark.spawns_process

_KEYS = {"mode", "sign", "paths", "head", "committed", "sha", "parent_ms", "children_ms", "spawns", "phases"}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.fixture
def tiny_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "probe")
    _git(repo, "config", "user.email", "probe@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    for i in range(4):
        (repo / f"f{i}.md").write_text(f"v{i}\n", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_report_pins_keys_and_commit(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 3, "off")
    assert _KEYS <= set(rep)
    assert rep["committed"] is True and rep["sha"]
    assert rep["mode"] == "phased" and rep["sign"] == "off" and rep["paths"] == 3
    assert rep["head"] != rep["sha"]
    assert rep["parent_ms"] >= 0 and rep["children_ms"] >= 0 and rep["spawns"] >= 0
    assert rep["phases"], "no phase was bracketed"
    for name, value in rep["phases"].items():
        assert isinstance(value, float) and value >= 0, name


def test_sign_off_writes_clone_local_config(tiny_repo):
    _git(tiny_repo, "config", "commit.gpgsign", "true")
    clp.build_report(tiny_repo, "phased", 1, "off")
    out = subprocess.run(["git", "-C", str(tiny_repo), "config", "--local", "commit.gpgsign"],
                         capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    assert out.strip() == "false"


def test_rejects_unknown_arms(tiny_repo):
    with pytest.raises(ValueError):
        clp.build_report(tiny_repo, "bogus", 1, "off")
    with pytest.raises(ValueError):
        clp.build_report(tiny_repo, "phased", 1, "bogus")
