"""A checkpoint landed in the engine's real shape (product plus process halves, an untracked
plan, a later halt commit) reads coded, and close reconciliation finds no open row."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.execute_plan_assemble.close_out_and_stamp import _open_blocking_chunk_ids
from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_PLAN = "docs/plans/p.md"
_PRODUCT = "src/row_a.txt"
_PROCESS = "state/sizings/row_a.txt"
_SESSION = "7173d553-49e6-4a96-85a0-60ecb3d359f4"
_DELIVERABLE = "dlv-real-shape-0001"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


def _w(path, text, newline="\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline=newline)


def _plan_text() -> str:
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n"
        for i in ("C1", "C2")
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _spine(text: str) -> list:
    block = text.replace("\r\n", "\n").split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)


@pytest.fixture(params=["lf", "crlf"])
def repo_eol(request):
    return request.param


@pytest.fixture()
def repo(repo_eol, tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _git(r, "config", "core.autocrlf", "false")
    _w(r / "seed.txt", "x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    # The plan is written after the seed commit and never added: untracked at checkpoint time.
    text = _plan_text()
    _w(r / _PLAN, text.replace("\n", "\r\n") if repo_eol == "crlf" else text, newline="")
    return r


def _checkpoint_message(base: str) -> str:
    # Composed as emit._checkpoint_commit_js: subject, then the Checkpoint-Plan/Base trailers, then
    # the Session-Id / Deliverable-Id trailers the committer route attaches.
    return (
        "checkpoint(wave 0): 2 rows — C1, C2"
        f"\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: {base}\nCheckpoint-Not-Landed: run-open"
        f"\nSession-Id: {_SESSION}\nDeliverable-Id: {_DELIVERABLE}\n"
    )


def test_a_real_shape_checkpoint_reads_coded_and_close_finds_no_open_row(repo, repo_eol):
    base = _git(repo, "rev-parse", "HEAD")
    _w(repo / _PRODUCT, "row\n")
    _w(repo / _PROCESS, "handoff\n")

    out = commit_v2._handler(
        {
            "paths": [_PRODUCT, _PROCESS],
            "skip_missing": True,
            "gone_tracked_as_deleted": True,
            "message": _checkpoint_message(base),
        },
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    sha = out["sha"]
    assert _git(repo, "show", "--name-only", "--format=", sha).split() == [_PRODUCT]
    assert _git(repo, "log", "-1", "--format=%s", "HEAD").endswith("(process files)")
    assert out["checkpoint_stamp"] == {"stamp_deferred": "run-open"}, out["checkpoint_stamp"]

    on_disk = (repo / _PLAN).read_bytes().decode("utf-8")
    for row in _spine(on_disk):
        assert row["disposition"] == "open", row
    assert ("\r\n" in on_disk) == (repo_eol == "crlf")

    halt = commit_v2._handler(
        {
            "paths": [_PLAN],
            "message": (
                "warp: record halted run artifacts\n\n"
                f"Halted-By: ledger-check\nHalt-Plan: {_PLAN}\nLands-Artifacts: plan=1\n"
            ),
        },
        repo_root=repo / ".git",
    )
    assert halt["committed"] is True, halt

    committed = _git(repo, "show", f"{halt['sha']}:{_PLAN}")
    assert _open_blocking_chunk_ids(_spine(committed)) == ["C1", "C2"]
