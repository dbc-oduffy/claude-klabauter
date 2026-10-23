"""dispatch.terminal_commit composes ``release_held`` with ``withhold_paths``: a drifted path named in
``withhold_paths`` stays dirty and unrefused while the released held chunk lands."""

from __future__ import annotations

import hashlib

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- fixture
    SID,
    _git,
    _record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_TEXT = {"a.py": "A\n", "shared.py": "S\n", "b.py": "B\n"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fire(repo, drifted: str, **params) -> dict:
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=("a.py", "shared.py")),
        ChunkCommit(id="C2", title="t2", paths=("shared.py", "b.py")),
    ))
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    for rel, text in _TEXT.items():
        (repo / rel).write_text("DRIFTED\n" if rel == drifted else text, encoding="utf-8", newline="")
        _record(repo, rel, _sha(text))
    from coordinator_core.ops.dispatch_emit import run_token

    token = run_token.mint(repo, "run-1", "run.mjs")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": REVIEWED,
         "session_id": SID, "run_id": "run-1", "run_token": token, **params},
        repo_root=repo / ".git",
    )


def _landed(repo) -> set:
    out = _git(["log", "--name-only", "--format="], repo)
    return {ln for ln in out.splitlines() if ln.strip()} - {"README.md"}


HELD = [{"chunk": "C2", "reason": "held"}]


def _withhold(path: str) -> list:
    return [{"path": path, "reason": "blob-drift: bytes changed after the run's review"}]


def test_release_held_alone_still_refuses_the_drift(repo):
    out = _fire(repo, "a.py", held=HELD, release_held=["C2"])
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"


def test_withhold_alone_still_refuses_the_held_landed_overlap(repo):
    out = _fire(repo, "a.py", held=HELD, withhold_paths=_withhold("a.py"))
    assert out["committed"] is False
    assert out["refused"] == "held-landed-overlap"


@pytest.mark.parametrize("drifted", ["a.py", "b.py"])
def test_release_held_and_withhold_compose(repo, drifted):
    out = _fire(repo, drifted, held=HELD, release_held=["C2"], withhold_paths=_withhold(drifted))
    assert out["committed"] is True, out
    landed = _landed(repo)
    assert {"a.py", "b.py", "shared.py"} - {drifted} <= landed
    assert drifted not in landed
    assert f"?? {drifted}" in _git(["status", "--porcelain"], repo).splitlines()
