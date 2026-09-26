"""Chunk commits a dispatched committer landed without a `Session-Id` trailer.

`decide_review_scale` selects this session's commits by `Session-Id` trailer,
which is the only sound peer-exclusion selector on a shared branch (see
`_session_owned_shas`'s own docstring). But the trailer names the COMMITTER,
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
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, **_NO_WINDOW)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, **_NO_WINDOW)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True, **_NO_WINDOW)


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

    recoverable, conflicting = wsc._dispatched_chunk_shas_missing_from_slices(
        tmp_path, _plan(tmp_path, "C3", sha), set()
    )

    assert recoverable == [{"chunk": "C3", "sha": sha}]
    assert conflicting == []


def test_chunk_commit_already_sliced_is_not_double_counted(tmp_path):
    _init_git_repo(tmp_path)
    sha = _commit_without_trailer(tmp_path, "chunk.py")

    recoverable, conflicting = wsc._dispatched_chunk_shas_missing_from_slices(
        tmp_path, _plan(tmp_path, "C3", sha), {sha}
    )

    assert recoverable == []
    assert conflicting == []


def test_foreign_trailered_chunk_commit_is_surfaced_not_folded_in(tmp_path):
    _init_git_repo(tmp_path)
    peer = "99999999-9999-4999-8999-999999999999"
    sha = _commit_with_foreign_trailer(tmp_path, "peer.py", peer)

    recoverable, conflicting = wsc._dispatched_chunk_shas_missing_from_slices(
        tmp_path, _plan(tmp_path, "C4", sha), set()
    )

    assert recoverable == []
    assert conflicting == [{"chunk": "C4", "sha": sha, "committed_by": peer}]


def test_unresolvable_disposition_ref_is_skipped_by_both_arms(tmp_path):
    _init_git_repo(tmp_path)

    recoverable, conflicting = wsc._dispatched_chunk_shas_missing_from_slices(
        tmp_path, _plan(tmp_path, "C9", "0" * 40), set()
    )

    assert recoverable == []
    assert conflicting == []


def test_open_row_contributes_no_slice(tmp_path):
    _init_git_repo(tmp_path)
    sha = _commit_without_trailer(tmp_path, "chunk.py")

    recoverable, conflicting = wsc._dispatched_chunk_shas_missing_from_slices(
        tmp_path, _plan(tmp_path, "C1", sha, disposition="open"), set()
    )

    assert recoverable == []
    assert conflicting == []


def test_absent_governing_plan_is_not_an_error(tmp_path):
    _init_git_repo(tmp_path)

    assert wsc._dispatched_chunk_shas_missing_from_slices(tmp_path, None, set()) == ([], [])
