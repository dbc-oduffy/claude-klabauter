"""A `--type sizing-object` scaffold refuses to overwrite an existing sizing object.

B4a (docs/plans/2026-09-26-inbox-blitz-part-b-engine-defects.md): a truncated-slug
collision (two titles hashing to the same slug prefix) could silently replace a
ratified sizing object at the same default path with a fresh, unrelated scaffold.
The loss is a routing-lobby record joined to a deliverable, not a regenerable file.

The refusal is scoped to --type sizing-object only — other doc types rely on
conform-in-place re-invocation at the same default path, which a general
existence refusal would break.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", ".")
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI_PATH), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_second_scaffold_at_same_path_is_refused_and_first_is_unchanged(repo: Path):
    first = _run(repo, "--type", "sizing-object", "--title", "A real PM ask")
    assert first.returncode == 0, first.stderr

    minted = list((repo / "state" / "sizings").glob("*.yaml"))
    assert len(minted) == 1
    out_path = minted[0]
    original_bytes = out_path.read_bytes()

    second = _run(repo, "--type", "sizing-object", "--title", "A real PM ask")

    assert second.returncode != 0
    assert "refusing to overwrite" in second.stderr
    assert str(out_path) in second.stderr or out_path.name in second.stderr
    assert "--out" in second.stderr
    assert out_path.read_bytes() == original_bytes
    assert len(list((repo / "state" / "sizings").glob("*.yaml"))) == 1


def test_an_explicit_out_still_scaffolds_alongside_the_existing_one(repo: Path):
    first = _run(repo, "--type", "sizing-object", "--title", "A real PM ask")
    assert first.returncode == 0, first.stderr

    other_path = repo / "state" / "sizings" / "explicit-out.yaml"
    second = _run(
        repo,
        "--type", "sizing-object",
        "--title", "A different PM ask",
        "--out", str(other_path),
    )

    assert second.returncode == 0, second.stderr
    assert other_path.exists()
