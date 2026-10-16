"""dispatch.terminal_commit refuses, before commit_v2, a landing set carrying an unparseable
``.py`` or a blob that drops lines a foreign commit added since the run base."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, path: str, **extra) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(path,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []}
    params.update(extra)
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_an_unparseable_py_is_refused_with_the_named_reason(repo):
    (repo / "m.py").write_text('x = "a\nb"\n', encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "m.py")

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.PY_UNPARSEABLE
    assert out["integrity"][0]["path"] == "m.py"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_stubbed_overlap_finding_is_refused_and_the_token_is_released(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)
    seen = {}

    def stub(root, paths, base, *, run_id):
        seen["base"] = base
        return [landing_integrity.Finding("ok.py", landing_integrity.SINCE_BASE_OVERLAP, "drops abc1234")]

    monkeypatch.setattr(landing_integrity, "since_base_findings", stub)
    token = run_token.mint(repo, "r1", "run.mjs")

    out = _fire(repo, "ok.py", run_base_sha=head, run_id="r1", run_token=token)

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.SINCE_BASE_OVERLAP
    assert seen["base"] == head
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert run_token.admits(repo, "r1", token, "run.mjs") is True


def test_a_clean_landing_without_a_base_commits_and_reports_no_base(repo):
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")

    out = _fire(repo, "ok.py")

    assert out["committed"] is True, out
    assert out["since_base_check"] == "no-base"
