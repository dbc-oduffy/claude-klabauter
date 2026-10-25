"""A terminal commit whose ledger names an untracked directory in a post_commit `commit` row lands
every file under it, or refuses before writing; it never reports committed with the directory absent."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    DLV,
    PLAN,
    RUN,
    _git,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPORT = "reports/C1.md"
DIRNAME = "ladder/r2"


def _declare_prefix_chunk(root) -> None:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=(), prefixes=("ladder/",), report=REPORT),),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    _write(root, REPORT, "DONE\ncreated-under-prefix: none\n")


def _verdict(evidence: str, acts=("commit",)) -> dict:
    return {"items": [
        {"id": "R1", "status": "post_commit", "post_commit_acts": list(acts), "evidence": evidence, "wired": True},
        {"id": "R2", "status": "not_met", "withheld_by": "C9"},
        {"id": "R3", "status": "not_met", "essential": False},
    ]}


def _call(root, verdict) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": verdict, "run_id": RUN,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _tree(root) -> list:
    return _git(root, "ls-tree", "-r", "--name-only", "HEAD").split()


def _seed_dir(root) -> None:
    for n in range(3):
        _write(root, f"{DIRNAME}/f{n}.txt", f"file {n}\n")


def test_an_untracked_dir_named_by_a_commit_row_lands_whole_or_the_commit_refuses(repo):  # noqa: F811
    _declare_prefix_chunk(repo)
    _seed_dir(repo)

    out = _call(repo, _verdict(f"{DIRNAME} present untracked with 3 files"))

    assert out["committed"] is True, out
    landed = _tree(repo)
    assert all(f"{DIRNAME}/f{n}.txt" in landed for n in range(3)), landed


def test_a_file_under_the_dir_that_cannot_land_refuses_before_any_write(repo):  # noqa: F811
    _declare_prefix_chunk(repo)
    _seed_dir(repo)
    before = _git(repo, "rev-parse", "HEAD")
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "run_id": RUN,
        "ledger_verdict": _verdict(f"{DIRNAME} present untracked with 3 files"),
        "withhold_paths": [{"path": f"{DIRNAME}/f1.txt", "reason": "operator holds it"}],
    }

    out = terminal_commit._handler(params, repo_root=repo / ".git")

    assert out["committed"] is False
    assert out["refused"] == "post-commit-path-unlanded"
    assert f"{DIRNAME}/f1.txt" in out["unlanded"]
    assert _git(repo, "rev-parse", "HEAD") == before


def test_a_commit_row_naming_no_path_adds_no_obligation(repo):  # noqa: F811
    _declare_prefix_chunk(repo)
    _seed_dir(repo)

    out = _call(repo, _verdict("the run's own declared writes"))

    assert out.get("refused") != "post-commit-path-unlanded"
    assert not any(p.startswith(DIRNAME) for p in _tree(repo))


def test_a_non_commit_act_row_adds_no_obligation(repo):  # noqa: F811
    _declare_prefix_chunk(repo)
    _seed_dir(repo)

    out = _call(repo, _verdict(f"{DIRNAME} present untracked", acts=("sha_reply",)))

    assert out.get("refused") != "post-commit-path-unlanded"
    assert not any(p.startswith(DIRNAME) for p in _tree(repo))
