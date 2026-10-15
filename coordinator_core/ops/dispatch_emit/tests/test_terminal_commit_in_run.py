"""dispatch.terminal_commit run in-workflow: the run token, the docs stage's files, and the
sizing's `shipped` resolution with its SHA."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_SIZING = "state/sizings/20260101-a.yaml"
_PLAN = "docs/plans/p.md"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    sizing = root / _SIZING
    sizing.parent.mkdir(parents=True)
    sizing.write_text(
        "\n".join(
            [
                "schema: sizing-object",
                "intent: Test intent, verbatim.",
                "estimate:",
                "  tshirt: XS",
                "  provisional: true",
                "route: dispatch",
                "detents: []",
                "fork: null",
                "xl_exit: null",
                "status: routed",
                "premise:",
                "  provenance: read",
                "  evidence: test fixture",
                "",
            ]
        ),
        encoding="utf-8",
    )
    plan = root / _PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nsizing_object: {_SIZING}\n---\n# p\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _call(repo: Path, docs: tuple = (), **extra) -> dict:
    chunks = (ChunkCommit(id="C1", title="t1", paths=("a.py",)),)
    if docs:
        chunks += (ChunkCommit(id="docs", title="run docs", paths=docs),)
    request = CommitRequest(chunks=chunks, plan_path=_PLAN)
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    params = {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **extra}
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_a_token_not_minted_for_the_run_refuses_before_any_write(repo):
    out = _call(repo, run_id="ask-1", run_token="0" * 32)
    assert out["committed"] is False and out["refused"] == "run-token"
    assert "a.py" not in _git(repo, "log", "--stat", "-1")


def test_minted_token_commits_and_is_redeemed(repo):
    token = run_token.mint(repo, "ask-1", "run.mjs")
    out = _call(repo, run_id="ask-1", run_token=token)
    assert out["committed"] is True, out
    assert run_token.admits(repo, "ask-1", token, "run.mjs") is False
    assert run_token.redeem(repo, "ask-1", token, "run.mjs") is False


def test_a_token_the_guard_already_claimed_still_redeems_once(repo):
    token = run_token.mint(repo, "ask-1", "run.mjs")
    assert run_token.admits(repo, "ask-1", token, "run.mjs")
    assert _call(repo, run_id="ask-1", run_token=token)["committed"] is True
    assert _call(repo, run_id="ask-1", run_token=token)["refused"] == "run-token"


def test_declared_doc_writes_ride_the_commit_and_residue_does_not(repo):
    note = repo / "docs" / "delivery" / "ask-1.md"
    note.parent.mkdir(parents=True)
    note.write_text("# delivered\n", encoding="utf-8")
    residue = repo / "scratch" / "warp" / "ask-1" / "briefs" / "C1.md"
    residue.parent.mkdir(parents=True)
    residue.write_text("brief\n", encoding="utf-8")
    out = _call(repo, docs=("docs/delivery/ask-1.md",))
    assert out["committed"] is True, out
    landed = _git(repo, "show", "--name-only", "--format=", out["sha"])
    assert "docs/delivery/ask-1.md" in landed and "scratch/" not in landed
    # The commit takes no caller-named doc list: only the stage's declared writes land.
    assert "doc_paths" not in {f.name for f in terminal_commit._PARAM_FIELDS}


def test_landed_run_ships_its_sizing_with_the_sha_in_a_trailer(repo):
    out = _call(repo)
    assert out["committed"] is True, out
    assert out["sizing_shipped"] == {"sizing": _SIZING, "sha": out["sha"]}
    assert "status: shipped" in (repo / _SIZING).read_text(encoding="utf-8")
    import yaml

    assert yaml.safe_load((repo / _SIZING).read_text(encoding="utf-8"))["resolution"] == {
        "sha": out["sha"], "run_id": "run",
    }
    log = _git(repo, "log", "--format=%B", "-1", "--", _SIZING)
    assert f"Sizing-Shipped: {_SIZING} {out['sha']}" in log
    assert _git(repo, "status", "--porcelain", "--", _SIZING) == ""


def test_an_incomplete_run_leaves_the_sizing_open(repo):
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)), ChunkCommit(id="C2", title="t2", paths=("b.py",))),
        plan_path=_PLAN,
    )
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": ["C2"], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    assert "sizing_shipped" not in out
    assert "status: routed" in (repo / _SIZING).read_text(encoding="utf-8")


def test_landed_run_records_its_resolution_off_the_commit(repo):
    import json as _json

    out = _call(repo)
    assert out["committed"] is True, out
    product = _git(repo, "log", "--format=%B", "-1", out["sha"])
    assert f"Resolves-Sizing: 20260101-a XS" in product
    rows = (repo / terminal_commit.SIZING_LEDGER_REL).read_text(encoding="utf-8").splitlines()
    row = _json.loads(rows[-1])
    assert row["resolved_sha"] == out["sha"] and row["tshirt"] == "XS" and row["route"] == "dispatch"
    assert len(row["ask_sha256"]) == 64
    assert terminal_commit.SIZING_LEDGER_REL not in _git(repo, "show", "--name-only", "--format=", out["sha"])
