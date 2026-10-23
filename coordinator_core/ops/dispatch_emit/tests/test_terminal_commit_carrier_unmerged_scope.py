"""An unmerged carrier close grades only its own rows; the marker's sibling_scope rows stay open."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    SIBLINGS_MERGED_PARAM,
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plan.md"
IDS = ("A1", "A2", "B1", "B2")
OWN = ("A1", "A2")
SIBLINGS = ("B1", "B2")
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _plan_text() -> str:
    rows = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n"
        f"  disposition: open\n  deferred: false\n"
        for i in IDS
    )
    return f"---\ntitle: p\ncreated: 2020-01-01\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _rows(root: Path) -> dict:
    text = (root / PLAN).read_text(encoding="utf-8")
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block(text).body)}


def _open(root: Path) -> set:
    return {i for i, r in _rows(root).items() if _row_disposition(r) == "open"}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    _git(["init", "-q"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    (root / PLAN).write_text(_plan_text(), encoding="utf-8", newline="\n")
    for i in IDS:
        (root / f"{i.lower()}.py").write_text("v0\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    # A1 and A2 are the carrier's own landed writes; B1 is a sibling's dirty, unfinished write.
    (root / "a1.py").write_text("v1\n", encoding="utf-8")
    (root / "a2.py").write_text("v1\n", encoding="utf-8")
    (root / "b1.py").write_text("half-done\n", encoding="utf-8")
    return root


def _request(sibling_scope=SIBLINGS) -> CommitRequest:
    return CommitRequest(
        chunks=tuple(ChunkCommit(id=i, title=f"t{i}", paths=(f"{i.lower()}.py",)) for i in IDS),
        plan_path=PLAN,
        sibling_scope=tuple(sibling_scope),
    )


def _fire(root: Path, incomplete=(), merged=(), request=None, **extra) -> dict:
    (root / "run.mjs").write_text(render_marker(request or _request()) + "\n", encoding="utf-8")
    params = {
        "inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": list(incomplete),
        **extra,
    }
    if merged:
        params[SIBLINGS_MERGED_PARAM] = list(merged)
    return terminal_commit._handler(params, repo_root=root / ".git")


def _committed_files(root: Path, sha: str) -> set:
    return set(_git(["show", "--name-only", "--format=", sha], root).split())


def test_unmerged_carrier_close_stamps_only_its_own_rows(repo):
    out = _fire(repo)

    assert out["committed"] is True, out
    assert _open(repo) == set(SIBLINGS), out
    assert out["sibling_rows_unmerged"] == list(SIBLINGS)
    assert out.get("plan_status") != "implemented"
    assert "status: implemented" not in (repo / PLAN).read_text(encoding="utf-8")
    assert "b1.py" not in _committed_files(repo, out["sha"])


def test_unmerged_close_keeps_a_sibling_checkpoint_from_coding_its_row(repo):
    base = _git(["rev-parse", "HEAD"], repo)
    (repo / "b1.py").write_text("sib\n", encoding="utf-8")
    _git(["add", "b1.py"], repo)
    _git(
        ["commit", "-q", "-m", "checkpoint(wave 0): 1 rows — B1",
         "-m", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: {base[:7]}"],
        repo,
    )

    out = _fire(repo)

    assert out["committed"] is True, out
    assert "B1" in _open(repo) and "B2" in _open(repo), out


def test_merged_digest_grades_the_commit_scope_rows(repo):
    out = _fire(repo, incomplete=["B1"], merged=["sib0", "sib1"])

    assert out["committed"] is True, out
    assert "sibling_rows_unmerged" not in out
    assert _open(repo) == {"B1"}, out


def test_marker_without_sibling_scope_closes_as_before(repo):
    out = _fire(repo, request=_request(sibling_scope=()))

    assert out["committed"] is True, out
    assert "sibling_rows_unmerged" not in out
    assert _open(repo) == set(), out


def test_unmerged_sibling_row_with_a_done_report_does_not_land(repo):
    (repo / "b1-report.md").write_text("DONE: B1\nFiles changed: b1.py\n", encoding="utf-8")
    req = _request()
    req = dataclasses.replace(
        req,
        chunks=tuple(
            dataclasses.replace(c, report="b1-report.md") if c.id == "B1" else c for c in req.chunks
        ),
    )

    out = _fire(repo, request=req)

    assert out["committed"] is True, out
    assert "B1" in _open(repo)
    assert out["sibling_rows_unmerged"] == list(SIBLINGS)
    assert "b1.py" not in _committed_files(repo, out["sha"])
