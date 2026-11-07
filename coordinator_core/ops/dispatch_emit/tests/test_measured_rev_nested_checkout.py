"""A measured_rev call whose root sits inside a nested checkout records into the run's own repo."""

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

RUN = "ask-nested-1"


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
        ("config", "core.autocrlf", "false"),
    ):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "f.txt").write_bytes(b"x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)


@pytest.fixture
def live_and_clone(tmp_path):
    live = tmp_path / "live"
    _init(live)
    (live / RECORD_DIR_ROOT / RUN).mkdir(parents=True)
    clone = live / "scratch" / "clone"
    _init(clone)
    return live, clone


def test_handler_with_ipc_root_in_nested_clone_records_in_live_repo(live_and_clone):
    live, clone = live_and_clone
    out = mr._handler({"run_id": RUN, "paths": ["f.txt"]}, clone / ".git")
    assert "error" not in out
    assert (live / RECORD_DIR_ROOT / RUN / "measured-rev.json").is_file()
    assert not (clone / RECORD_DIR_ROOT / RUN).exists()


def test_cwd_default_inside_nested_clone_records_in_live_repo(live_and_clone, monkeypatch):
    live, clone = live_and_clone
    monkeypatch.chdir(clone)
    mr.measured_rev(RUN, ["f.txt"])
    assert (live / RECORD_DIR_ROOT / RUN / "measured-rev.json").is_file()
    assert not (clone / RECORD_DIR_ROOT / RUN).exists()


def test_clone_that_owns_the_run_keeps_its_own_root(live_and_clone):
    live, clone = live_and_clone
    (clone / RECORD_DIR_ROOT / RUN).mkdir(parents=True)
    mr.measured_rev(RUN, ["f.txt"], repo_root=clone)
    assert (clone / RECORD_DIR_ROOT / RUN / "measured-rev.json").is_file()
    assert not (live / RECORD_DIR_ROOT / RUN / "measured-rev.json").exists()


def test_run_unknown_to_every_enclosing_checkout_stays_in_given_root(tmp_path):
    live = tmp_path / "live"
    _init(live)
    clone = live / "scratch" / "clone"
    _init(clone)
    mr.measured_rev("fresh-run", ["f.txt"], repo_root=clone)
    assert (clone / RECORD_DIR_ROOT / "fresh-run" / "measured-rev.json").is_file()
