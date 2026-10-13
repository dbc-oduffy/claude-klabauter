"""Chunk commits a dispatched committer landed without a `Session-Id` trailer.

`decide_review_scale` selects this session's commits by `Session-Id` trailer,
which is the only sound peer-exclusion selector on a shared branch (the
`SessionHistory.own` subset). But the trailer names the COMMITTER,
and a chunk committed by a git-commit-agent dispatched inside a background
Workflow is committed by that agent's own process: `commit_trailers.
_resolve_session_id` omits rather than guesses, so the commit lands with NO
trailer and drops out of `commit_slices` silently.

Measured 2026-08-31 on `work/machine-a/2026-08-18to31`: `ed344842186b` (chunk C3,
5 files, 309 insertions -- the largest chunk of its workstream) was absent from
`commit_slices` while `1f5dc47b9a`, committed by the EM minutes earlier on the
same branch, was present. The gate still reported `resolved: true,
partition_mandatory: true` over the narrowed set, which is worse than refusing:
the close reads fully reviewed while the biggest diff reached no reviewer, and
"slice, never narrow" cannot defend against a narrowing nobody is told about.

Lives in its own module rather than appended to `test_workstream_complete.py`
because that file is routinely co-edited by concurrent sessions on this shared
worktree, and a pathspec scopes which FILES land, not which hunks.

The gap is read off `review_scale_inputs.measure_review_scale_inputs(...).attribution_gap`
over the session's `head_at_start..HEAD` window, with the chunk refs from `_coded_chunk_refs`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.workstream_complete import review_scale_inputs, session_history

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_OWN = "11111111-1111-4111-8111-111111111111"


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, **_NO_WINDOW)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True, **_NO_WINDOW)
    claim = root / ".git" / "coordinator-sessions" / _OWN
    claim.mkdir(parents=True)
    (claim / "head_at_start").write_text(_head(root), encoding="utf-8")


def _gap(root: Path, governing) -> "tuple[list, list]":
    """`(recoverable, conflicting)` for `governing` over the session's own window."""
    history = session_history.read_session_history(root, _OWN)
    assert history.resolved, history.reason
    refs, deliverable = wsc._coded_chunk_refs(governing)
    gap = review_scale_inputs.measure_review_scale_inputs(
        root, history, [], chunk_refs=refs, plan_deliverable_id=deliverable
    ).attribution_gap
    return gap["recovered"], gap["conflicting"]


def _head(root: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True, **_NO_WINDOW
    )
    return out.stdout.strip()


def _commit_without_trailer(root: Path, name: str) -> str:
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(["git", "commit", "-q", "-m", f"add {name}"], cwd=root, check=True, **_NO_WINDOW)
    return _head(root)


def _commit_with_foreign_trailer(root: Path, name: str, sid: str) -> str:
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"add {name}", "--trailer", f"Session-Id: {sid}"],
        cwd=root,
        check=True,
        **_NO_WINDOW,
    )
    return _head(root)


def _plan(root: Path, chunk_id: str, sha: str, disposition: str = "coded"):
    plan = root / "plan.md"
    plan.write_text(
        "---\ntitle: t\n---\n\n## Tasks\n\n"
        "```yaml plan-tasks\n"
        f"- id: {chunk_id}\n"
        f"  title: shipped chunk\n"
        f"  disposition: {disposition}\n"
        f"  disposition_ref: {sha}\n"
        "```\n",
        encoding="utf-8",
    )

    class _GoverningPlan:
        path = plan

    return _GoverningPlan()


def test_untrailered_chunk_commit_is_recovered_into_review_scope(tmp_path):
    _init_git_repo(tmp_path)
    sha = _commit_without_trailer(tmp_path, "chunk.py")

    recoverable, conflicting = _gap(tmp_path, _plan(tmp_path, "C3", sha))

    assert recoverable == [{"chunk": "C3", "sha": sha}]
    assert conflicting == []


def test_chunk_commit_already_sliced_is_not_double_counted(tmp_path):
    _init_git_repo(tmp_path)
    sha = _commit_with_foreign_trailer(tmp_path, "chunk.py", _OWN)

    plan = _plan(tmp_path, "C3", sha)
    recoverable, conflicting = _gap(tmp_path, plan)

    assert recoverable == []
    assert conflicting == []
    history = session_history.read_session_history(tmp_path, _OWN)
    refs, deliverable = wsc._coded_chunk_refs(plan)
    measured = review_scale_inputs.measure_review_scale_inputs(
        tmp_path, history, [], chunk_refs=refs, plan_deliverable_id=deliverable
    )
    assert [s["sha"] for s in measured.commit_slices] == [sha]


def test_chunk_commit_before_the_session_floor_is_neither_recovered_nor_conflicting(tmp_path):
    """The window is the session's own span: a ref outside it is another session's work."""
    _init_git_repo(tmp_path)
    older = _commit_without_trailer(tmp_path, "older.py")
    claim = tmp_path / ".git" / "coordinator-sessions" / _OWN
    (claim / "head_at_start").write_text(older, encoding="utf-8")
    _commit_without_trailer(tmp_path, "later.py")

    recoverable, conflicting = _gap(tmp_path, _plan(tmp_path, "C2", older))

    assert recoverable == []
    assert conflicting == []


def test_foreign_trailered_chunk_commit_is_surfaced_not_folded_in(tmp_path):
    _init_git_repo(tmp_path)
    peer = "99999999-9999-4999-8999-999999999999"
    sha = _commit_with_foreign_trailer(tmp_path, "peer.py", peer)

    recoverable, conflicting = _gap(tmp_path, _plan(tmp_path, "C4", sha))

    assert recoverable == []
    assert conflicting == [{"chunk": "C4", "sha": sha, "committed_by": peer}]


def test_unresolvable_disposition_ref_is_skipped_by_both_arms(tmp_path):
    _init_git_repo(tmp_path)

    recoverable, conflicting = _gap(tmp_path, _plan(tmp_path, "C9", "0" * 40))

    assert recoverable == []
    assert conflicting == []


def test_open_row_contributes_no_slice(tmp_path):
    _init_git_repo(tmp_path)
    sha = _commit_without_trailer(tmp_path, "chunk.py")

    assert wsc._coded_chunk_refs(_plan(tmp_path, "C1", sha, disposition="open")) == ([], "")
    recoverable, conflicting = _gap(tmp_path, _plan(tmp_path, "C1", sha, disposition="open"))

    assert recoverable == []
    assert conflicting == []


def test_absent_governing_plan_is_not_an_error(tmp_path):
    _init_git_repo(tmp_path)

    assert wsc._coded_chunk_refs(None) == ([], "")
    assert _gap(tmp_path, None) == ([], [])


def _commit_with_trailers(root: Path, name: str, *trailers: str) -> str:
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True, **_NO_WINDOW)
    argv = ["git", "commit", "-q", "-m", f"add {name}"]
    for trailer in trailers:
        argv += ["--trailer", trailer]
    subprocess.run(argv, cwd=root, check=True, **_NO_WINDOW)
    return _head(root)


def _plan_with_deliverable(root: Path, deliverable_id: str, chunk_id: str, sha: str):
    governing = _plan(root, chunk_id, sha)
    text = governing.path.read_text(encoding="utf-8")
    governing.path.write_text(
        text.replace("title: t\n", f"title: t\ndeliverable_id: {deliverable_id}\n", 1),
        encoding="utf-8",
    )
    return governing


_PEER = "99999999-9999-4999-8999-999999999999"


def test_matching_deliverable_id_overrides_a_foreign_session_id(tmp_path):
    _init_git_repo(tmp_path)
    sha = _commit_with_trailers(
        tmp_path, "driver.py", f"Session-Id: {_PEER}", "Deliverable-Id: dlv-abc123"
    )

    recoverable, conflicting = _gap(tmp_path, _plan_with_deliverable(tmp_path, "dlv-abc123", "C4", sha))

    assert recoverable == [{"chunk": "C4", "sha": sha}]
    assert conflicting == []


def test_mismatched_deliverable_id_does_not_override_a_foreign_session_id(tmp_path):
    _init_git_repo(tmp_path)
    sha = _commit_with_trailers(
        tmp_path, "driver.py", f"Session-Id: {_PEER}", "Deliverable-Id: dlv-other9"
    )

    recoverable, conflicting = _gap(tmp_path, _plan_with_deliverable(tmp_path, "dlv-abc123", "C4", sha))

    assert recoverable == []
    assert conflicting == [{"chunk": "C4", "sha": sha, "committed_by": _PEER}]
