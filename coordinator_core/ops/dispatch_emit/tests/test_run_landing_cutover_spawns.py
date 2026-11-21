"""The cutover switch behind `dispatch.terminal_commit`: the settings-home key named by
`run_landing.LANDING_IMPL_KEY` routes a product landing to `land_run`; off, missing, unrecognised or
unreadable keeps the old path; the non-product modes never leave it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.dispatch_emit import run_landing, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ENV_KEY = machine_resolver._env_override_key(run_landing.LANDING_IMPL_KEY)
OLD_REPLY = {"routed": "old"}
NEW_REPLY = {"routed": "new"}
PRODUCT = {"script_path": "run.mjs", "inline_review": {"integration_stem": "s", "slices": []}}


@pytest.fixture
def routed(monkeypatch):
    """Both landing paths replaced by sentinels; returns the list of paths a call reached."""
    reached: list[str] = []
    monkeypatch.setattr(run_landing, "land_run", lambda p, r=None: reached.append("new") or NEW_REPLY)
    monkeypatch.setattr(terminal_commit, "_terminal_commit", lambda *a, **k: reached.append("old") or dict(OLD_REPLY))
    monkeypatch.delenv(ENV_KEY, raising=False)
    return reached


def _git(args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          **no_console_creationflags()).stdout.strip()


@pytest.mark.spawns_process
@pytest.mark.parametrize("impl", ["old", "new"])
def test_the_real_handler_lands_a_commit_on_either_path(impl, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv(ENV_KEY, impl)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                 ["config", "core.autocrlf", "false"]):
        _git(args, root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    before = _git(["rev-parse", "HEAD"], root)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="tC1", paths=("a.py",)),))
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": []}, repo_root=root / ".git"
    )
    assert out.get("committed") is True, out
    after = _git(["rev-parse", "HEAD"], root)
    assert after != before and out["sha"] == after
    assert _git(["show", "--name-only", "--format=", "HEAD"], root) == "a.py"
