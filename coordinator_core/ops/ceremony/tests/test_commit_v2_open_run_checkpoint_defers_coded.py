"""A checkpoint marked Checkpoint-Not-Landed leaves the plan bytes and dispositions untouched;
a legacy checkpoint without the marker still flips its rows to coded."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_PLAN = "docs/plans/p.md"
_PRODUCT = "src/row_a.txt"
_SESSION = "7173d553-49e6-4a96-85a0-60ecb3d359f4"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


def _plan_text() -> str:
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n"
        for i in ("C1", "C2")
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _dispositions(path) -> list:
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return [r["disposition"] for r in yaml.safe_load(block)]


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _git(r, "config", "core.autocrlf", "false")
    (r / "seed.txt").write_text("x\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    (r / "docs/plans").mkdir(parents=True)
    (r / _PLAN).write_text(_plan_text(), encoding="utf-8", newline="\n")
    return r


def _commit(repo, marker: str):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src").mkdir(exist_ok=True)
    (repo / _PRODUCT).write_text("row\n", encoding="utf-8", newline="\n")
    message = (
        "checkpoint(wave 0): 2 rows — C1, C2"
        f"\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: {base}{marker}"
        f"\nSession-Id: {_SESSION}\n"
    )
    return commit_v2._handler(
        {"paths": [_PRODUCT], "message": message}, repo_root=repo / ".git"
    )


def test_a_marked_checkpoint_leaves_plan_bytes_and_dispositions_unchanged(repo):
    before = (repo / _PLAN).read_bytes()
    out = _commit(repo, "\nCheckpoint-Not-Landed: run-open")
    assert out["committed"] is True, out
    assert out["checkpoint_stamp"] == {"stamp_deferred": "run-open"}, out["checkpoint_stamp"]
    assert (repo / _PLAN).read_bytes() == before
    assert _dispositions(repo / _PLAN) == ["open", "open"]


def test_a_legacy_checkpoint_without_the_marker_still_flips_coded(repo):
    out = _commit(repo, "")
    assert out["committed"] is True, out
    assert "stamp_deferred" not in out["checkpoint_stamp"], out["checkpoint_stamp"]
    assert _dispositions(repo / _PLAN) == ["coded", "coded"]
