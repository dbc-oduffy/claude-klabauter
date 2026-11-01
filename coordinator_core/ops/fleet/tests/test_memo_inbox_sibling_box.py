"""A memo delivered on one box's day branch is listed by `cross-repo-memo inbox` on another box.

Two clones of one bare origin sit on different `work/<machine>/<day>` branches; the push is
driven through `push_cadence.on_idle_tick`, the fetch in clone A is test setup only.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core import daily_day, machine_resolver
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _git as _run_git,
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)
from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CLI = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "cross-repo-memo.py"


def _git(cwd, *args) -> str:
    out = _run_git(cwd, *args).stdout
    return (out.decode() if isinstance(out, bytes) else out).strip()


def _load_cli():
    sys.path.insert(0, str(_CLI.parent))
    loader = importlib.machinery.SourceFileLoader("crm_sibling_box_under_test", str(_CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _cadence(monkeypatch):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    yield
    push_cadence.reset_cadence_for_test()


def _tick() -> bool:
    return push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 0.0, interval_secs=0.0
    ) or push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 1.0, interval_secs=0.0
    )


def _two_boxes(tmp_path: Path):
    """Receiver clone B and a sibling clone A of one bare origin, both on the migrated inbox layout."""
    sender = _make_sender_git_repo(tmp_path)
    box_b = _make_receiver_git_repo(tmp_path, name="box-b")
    inbox = box_b / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(box_b, "add", "-A")
    _git(box_b, "commit", "-q", "-m", "migrated inbox")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    _git(box_b, "remote", "add", "origin", str(bare))
    _git(box_b, "push", "origin", "main")
    box_a = tmp_path / "box-a"
    _git(tmp_path, "clone", "-q", str(bare), str(box_a))
    return sender, box_a, box_b


def _send_from_box_b(monkeypatch, sender: Path, box_b: Path, topic: str) -> str:
    monkeypatch.setattr(machine_resolver, "compute_machine", lambda: "boxb")
    _write_draft(sender, topic)
    result = _memo_send({"dry_run": False, "topic": topic}, repo_root=sender)
    assert result["exit_code"] == 0, result
    day = f"work/boxb/{daily_day.local_day(str(box_b))}"
    assert _git(box_b, "rev-parse", "--abbrev-ref", "HEAD") == day
    assert _tick()
    return day


def test_memo_sent_on_box_b_day_branch_is_listed_in_box_a_inbox(tmp_path, monkeypatch, capsys):
    sender, box_a, box_b = _two_boxes(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": box_b})))
    day_b = _send_from_box_b(monkeypatch, sender, box_b, "t-sibling")

    day_a = f"work/boxa/{daily_day.local_day(str(box_a))}"
    _git(box_a, "checkout", "-q", "-b", day_a)
    _git(box_a, "fetch", "-q", "origin")
    assert not list((box_a / "state" / "cross-repo" / "inbox").glob("*t-sibling.md"))

    monkeypatch.chdir(box_a)
    assert _load_cli()._cmd_inbox(argparse.Namespace()) == 0
    lines = capsys.readouterr().out.splitlines()

    stranded = [ln for ln in lines if ln.startswith("stranded  ") and "t-sibling" in ln]
    assert len(stranded) == 1, lines
    assert f"[on origin/{day_b}]" in stranded[0]
    assert lines[-1] == "1 memo(s), 0 open, 1 stranded"
    assert not list((box_a / "state" / "cross-repo" / "inbox").glob("*t-sibling.md"))
    assert _git(box_a, "status", "--porcelain") == ""


def test_memo_is_not_listed_on_the_box_that_holds_it_on_its_own_branch(tmp_path, monkeypatch, capsys):
    sender, _box_a, box_b = _two_boxes(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": box_b})))
    _send_from_box_b(monkeypatch, sender, box_b, "t-own")
    _git(box_b, "fetch", "-q", "origin")

    monkeypatch.chdir(box_b)
    assert _load_cli()._cmd_inbox(argparse.Namespace()) == 0
    lines = capsys.readouterr().out.splitlines()

    assert not [ln for ln in lines if ln.startswith("stranded  ")], lines
    assert [ln for ln in lines if ln.startswith("open  ") and "t-own" in ln], lines
