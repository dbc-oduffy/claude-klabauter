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


def _run(monkeypatch, repo: Path, env: dict[str, str], sid: str, extra: tuple[str, ...] = ()):
    for key in _ALL_MARKERS:
        monkeypatch.delenv(key, raising=False)
    from coordinator_core import warp_fire

    monkeypatch.setattr(warp_fire, "_counter_dir", lambda: repo.parent / "nag-counter")
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
    [{}, {"COORDINATOR_AGENT_PROVIDER": "codex", "COORDINATOR_AGENT_ROLE": "worker"}],
    ids=["claude-or-unmarked", "subagent"],
)
def test_claude_and_subagent_callers_get_no_advisory(tmp_path, monkeypatch, env):
    over = _repo(tmp_path, files=3, lines_each=13)
    stdout, _, _ = _run(monkeypatch, over, env, f"nag-none-{tmp_path.name}")
    assert "advisory" not in json.loads(stdout)
