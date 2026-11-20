"""coordinator_core.invoke.tests.test_invoke_diy_nag_per_family -- the op-response DIY nag, per EM family.

Purpose: a codex, grok or agy EM whose uncommitted hand edit is over the XS bound sees `advisory`
beside `result` on a worktree-scoped op; under the bound, or for a Claude/subagent caller, it
does not. `result` and the exit code are identical in every case.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_OP = "baton.close_report"

_FAMILY_ENV = {
    "codex": {"COORDINATOR_AGENT_PROVIDER": "codex"},
    "grok": {"COORDINATOR_AGENT_PROVIDER": "grok"},
    "agy": {"COORDINATOR_AGENT_PROVIDER": "agy"},
}
_ALL_MARKERS = (
    "COORDINATOR_AGENT_PROVIDER", "COORDINATOR_AGENT_VENDOR", "COORDINATOR_AGENT_ROLE",
    "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "AGY_SESSION_ID",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, creationflags=_NO_CONSOLE)


def _repo(tmp_path: Path, files: int, lines_each: int) -> Path:
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    for i in range(files):
        (repo / f"f{i}.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for i in range(files):
        (repo / f"f{i}.txt").write_text("base\n" + "x\n" * lines_each, encoding="utf-8")
    return repo


def _attribute_dirt_to_session(repo: Path, sid: str) -> None:
    """The nag measures only paths in the session's lane record; record every dirty path as its own edit."""
    from coordinator_core.session import xs_lane_budget

    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, creationflags=_NO_CONSOLE
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "-uall"], cwd=repo, check=True, capture_output=True, text=True,
        creationflags=_NO_CONSOLE,
    ).stdout.splitlines()
    xs_lane_budget.open_xs_lane(repo, sid, base)
    for row in dirty:
        xs_lane_budget.add_path(repo, sid, row[3:])


def _run(monkeypatch, repo: Path, env: dict[str, str], sid: str, extra: tuple[str, ...] = ()):
    for key in _ALL_MARKERS:
        monkeypatch.delenv(key, raising=False)
    from coordinator_core import warp_fire

    monkeypatch.setattr(warp_fire, "_counter_dir", lambda: repo.parent / "nag-counter")
    _attribute_dirt_to_session(repo, sid)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    stdout, stderr, code = _dispatch_argv(
        [_OP, "{}", "--repo", str(repo), "--allow-unstamped-dispatch", *extra], str(repo), allow_warm=False
    )
    return stdout, stderr, code


@pytest.mark.parametrize("family", ["codex", "grok", "agy"])
def test_over_bound_carries_advisory_and_leaves_result_alone(tmp_path, monkeypatch, family):
    over = _repo(tmp_path / "over", files=3, lines_each=13)
    stdout, _, code = _run(monkeypatch, over, _FAMILY_ENV[family], f"nag-over-{family}-{tmp_path.name}")
    reply = json.loads(stdout)
    assert reply.get("advisory"), (family, stdout)
    assert "result" in reply

    under = _repo(tmp_path / "under", files=1, lines_each=5)
    stdout2, _, code2 = _run(monkeypatch, under, _FAMILY_ENV[family], f"nag-under-{family}-{tmp_path.name}")
    reply2 = json.loads(stdout2)
    assert "advisory" not in reply2
    assert code == code2
    assert reply["result"].keys() == reply2["result"].keys()


def test_advisory_names_the_hand_edit_size(tmp_path, monkeypatch):
    over = _repo(tmp_path, files=3, lines_each=13)
    stdout, _, _ = _run(monkeypatch, over, _FAMILY_ENV["codex"], f"nag-size-{tmp_path.name}")
    assert "at least 3 files / 39 lines" in json.loads(stdout)["advisory"]


def _stamp(repo: Path, sid: str) -> Path:
    from coordinator_core.session import op_dispatch_nag, xs_lane_budget

    return xs_lane_budget.lane_path(repo, sid).parent / op_dispatch_nag._STAMP_FILENAME


def test_nag_window_is_stamped_once_the_advisory_is_printed(tmp_path, monkeypatch):
    over = _repo(tmp_path / "over", files=3, lines_each=13)
    sid = f"nag-stamp-{tmp_path.name}"
    stamp = _stamp(over, sid)
    stamp.unlink(missing_ok=True)
    stdout, _, _ = _run(monkeypatch, over, _FAMILY_ENV["codex"], sid)
    assert json.loads(stdout).get("advisory") and stamp.exists()

    under = _repo(tmp_path / "under", files=1, lines_each=5)
    sid2 = f"nag-nostamp-{tmp_path.name}"
    stamp2 = _stamp(under, sid2)
    stamp2.unlink(missing_ok=True)
    _run(monkeypatch, under, _FAMILY_ENV["codex"], sid2)
    assert not stamp2.exists()


def test_bare_mode_stamps_after_the_advisory_reaches_stderr(tmp_path, monkeypatch):
    over = _repo(tmp_path, files=3, lines_each=13)
    sid = f"nag-bare-stamp-{tmp_path.name}"
    stamp = _stamp(over, sid)
    stamp.unlink(missing_ok=True)
    _, stderr, _ = _run(monkeypatch, over, _FAMILY_ENV["agy"], sid, ("--bare",))
    assert stderr.strip() and stamp.exists()


def _repo_one_file(tmp_path: Path) -> Path:
    return _repo(tmp_path, files=1, lines_each=0)


def test_codex_large_tracked_hand_edit_gets_advisory(tmp_path, monkeypatch):
    repo = _repo_one_file(tmp_path)
    (repo / "f0.txt").write_text("base\n" + "x\n" * 525, encoding="utf-8")
    stdout, _, _ = _run(monkeypatch, repo, _FAMILY_ENV["codex"], f"nag-big-{tmp_path.name}")
    reply = json.loads(stdout)
    assert reply.get("advisory"), stdout
    assert "result" in reply


def test_codex_untracked_new_files_only_gets_advisory(tmp_path, monkeypatch):
    repo = _repo_one_file(tmp_path)
    for i in range(3):
        (repo / f"new{i}.py").write_text("x = 1\n" * 200, encoding="utf-8")
    stdout, _, _ = _run(monkeypatch, repo, _FAMILY_ENV["codex"], f"nag-untracked-{tmp_path.name}")
    reply = json.loads(stdout)
    assert reply.get("advisory"), stdout
    assert "result" in reply


def test_codex_small_untracked_file_gets_no_advisory(tmp_path, monkeypatch):
    repo = _repo_one_file(tmp_path)
    (repo / "new.py").write_text("x = 1\n" * 3, encoding="utf-8")
    stdout, _, _ = _run(monkeypatch, repo, _FAMILY_ENV["codex"], f"nag-small-{tmp_path.name}")
    assert "advisory" not in json.loads(stdout)


@pytest.mark.parametrize("family", ["codex", "grok", "agy"])
def test_bare_mode_keeps_stdout_the_result_and_advises_on_stderr(tmp_path, monkeypatch, family):
    over = _repo(tmp_path, files=3, lines_each=13)
    stdout, stderr, _ = _run(
        monkeypatch, over, _FAMILY_ENV[family], f"nag-bare-{family}-{tmp_path.name}", ("--bare",)
    )
    assert "advisory" not in json.loads(stdout)
    assert stderr.strip()


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"COORDINATOR_AGENT_PROVIDER": "codex", "COORDINATOR_AGENT_ROLE": "worker"},
        {"COORDINATOR_AGENT_PROVIDER": "codex", "COORDINATOR_AGENT_ROLE": "workflow-agent"},
    ],
    ids=["claude-or-unmarked", "subagent", "workflow-agent"],
)
def test_claude_and_subagent_callers_get_no_advisory(tmp_path, monkeypatch, env):
    over = _repo(tmp_path, files=3, lines_each=13)
    stdout, _, _ = _run(monkeypatch, over, env, f"nag-none-{tmp_path.name}")
    assert "advisory" not in json.loads(stdout)
