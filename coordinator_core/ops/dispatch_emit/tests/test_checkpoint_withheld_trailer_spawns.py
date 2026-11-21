"""Checkpoint-Withheld trailer: renderer, parser, and the stamp skipping a row whose declared path it names."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_PLAN = "docs/plans/p.md"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


def _plan_text() -> str:
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n" for i in ("C1", "C2")
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _dispositions(repo) -> dict:
    text = (repo / _PLAN).read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return {r["id"]: r["disposition"] for r in yaml.safe_load(block)}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    (r / "docs/plans").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / _PLAN).write_text(_plan_text(), encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    for i in ("C1", "C2"):
        (r / f"some/{i}.py").parent.mkdir(exist_ok=True)
        (r / f"some/{i}.py").write_text("v1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "row work")
    return r


def _message(extra: str = "") -> str:
    return f"checkpoint(wave 0): 2 rows — C1, C2\n\nCheckpoint-Plan: {_PLAN}\n{extra}"


@pytest.mark.spawns_process
def test_stamp_skips_the_row_whose_declared_path_was_withheld(repo) -> None:
    sha = _git(repo, "rev-parse", "HEAD")
    extra = "\n".join(cs.checkpoint_withheld_trailer_lines(["some/C1.py"])) + "\n"

    out = cs.stamp_checkpoint(repo, _message(extra), sha)

    assert out["rows_coded"] == {_PLAN: ["C2"]}
    assert _dispositions(repo) == {"C1": "open", "C2": "coded"}


@pytest.mark.spawns_process
def test_stamp_flips_every_row_when_nothing_was_withheld(repo) -> None:
    sha = _git(repo, "rev-parse", "HEAD")

    out = cs.stamp_checkpoint(repo, _message(), sha)

    assert out["rows_coded"] == {_PLAN: ["C1", "C2"]}


def test_stamp_flips_nothing_when_every_row_was_withheld(repo) -> None:
    sha = _git(repo, "rev-parse", "HEAD")
    extra = "\n".join(cs.checkpoint_withheld_trailer_lines(["some/C1.py", "some\\C2.py"])) + "\n"

    assert cs.stamp_checkpoint(repo, _message(extra), sha) == {}
    assert _dispositions(repo) == {"C1": "open", "C2": "open"}
