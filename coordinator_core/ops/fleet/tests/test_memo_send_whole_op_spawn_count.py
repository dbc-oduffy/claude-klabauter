"""
coordinator_core.ops.fleet.tests.test_memo_send_whole_op_spawn_count

ASSERTION and EXECUTION legs for `memo.send`'s composition-gate enrolment
(`_LEGITIMIZED_SITES`, keyed `"memo.send"`, in
`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py`).

Spec backlink: docs/plans/2026-09-12-memo-send-enrolled-in-the-composition-gate.md (C1)

Negative spec:
  - This is NOT the killed `test_memo_send_spawn_budget.py` (deleted whole by
    DR-344's kill bar, `c07062c99`; "kill means kill forever"). The functions
    it named do not exist in the rebuilt op and are not restored here.
  - Does NOT count through the `git_native._git` seam. `run_git`
    (`coordinator_core/git/run.py`) reaches git WITHOUT going through `_git`,
    so a `_git`-scoped counter undercounts (see
    `test_commit_authored_new_file.py`'s own module docstring for the exact
    incident: a `_git`-scoped counter read 3 while the leg issued 4
    processes). This file patches `subprocess.Popen` in the `subprocess`
    module itself, seam-independently, and does NOT also patch
    `subprocess.run` (which constructs a `Popen`, so patching both would
    double-count every `run()`-shaped spawn).
  - No budget number here was chosen to agree with any docstring's prose
    claim. Every `spawn_count_budget` value in the manifest and every
    ceiling in the ratchet table is the number this file's own counter
    measured on the fixtures below.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.git import git_state
from coordinator_core.ops.ceremony import git_native

from .test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


class _AttributedSpawn(NamedTuple):
    argv: tuple[str, ...]
    origin: str


#: `_LEGITIMIZED_SITES` key shape (`(relpath, enclosing, argv0, ordinal)`'s
_GIT_NATIVE_SUFFIX = "coordinator_core/ops/ceremony/git_native.py"
_RUN_GIT_SUFFIX = "coordinator_core/git/run.py"
_COMMIT_SIGNING_SUFFIX = "coordinator_core/git/commit_signing.py"


def _attribute_frame(frame) -> str | None:
    filename = Path(frame.f_code.co_filename).as_posix()
    if filename.endswith(_GIT_NATIVE_SUFFIX) and frame.f_code.co_name == "_invoke":
        return "_git._invoke"
    if filename.endswith(_RUN_GIT_SUFFIX) and frame.f_code.co_name == "run_git":
        return "run_git"
    if (
        filename.endswith(_COMMIT_SIGNING_SUFFIX)
        and frame.f_code.co_name == "write_signed_commit_object"
    ):
        return "write_signed_commit_object"
    return None


@contextmanager
def _count_spawns_attributed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_AttributedSpawn]]:
    recorded: list[_AttributedSpawn] = []
    real_popen = subprocess.Popen

    class _Popen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            origin = "unattributed"
            frame = __import__("sys")._getframe(1)
            while frame is not None:
                site = _attribute_frame(frame)
                if site is not None:
                    origin = site
                    break
                frame = frame.f_back
            argv = tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),)
            recorded.append(_AttributedSpawn(argv=argv, origin=origin))
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    try:
        yield recorded
    finally:
        monkeypatch.undo()


def _budget() -> dict:
    return load_manifest()["overrides"]["memo.send"]["spawn_count_budget"]


def test_green_path_spawn_count_matches_budget_and_is_attributed(tmp_path, monkeypatch):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "green-path-topic")

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _memo_send(
            {"dry_run": False, "topic": "green-path-topic"}, repo_root=sender_repo
        )

    assert result["exit_code"] == 0, result
    budget = _budget()
    assert len(spawns) == budget["green_path"], [s.origin for s in spawns]
    origins = [s.origin for s in spawns]
    assert "unattributed" not in origins, origins
    assert "_git._invoke" in origins, origins


def test_unreadable_head_spine_refusal_spawn_count_matches_budget_and_reaches_run_git(
    tmp_path, monkeypatch
):
    """PLANTED precondition: `git_native.read_tree_spine` monkeypatched to
    return `None`. No natural fixture in this file's `writes:` makes
    `read_tree_spine` return `None` (it requires a corrupt/unresolvable HEAD
    tree, not merely an absent path), so this follows
    `test_unmet_in_process_precondition_fails_loud_instead_of_taking_the_ladder`'s
    own precedent of monkeypatching the in-process precondition directly.

    `_head_entry_for` (called first, to check the path is absent from HEAD)
    falls back to `_git_state_head_blobs`, which reaches git through
    `run_git` -- NOT through `git_native._git` (census row 1/4). The commit
    then proceeds to `_commit_via_head_spine`, which ALSO consults
    `read_tree_spine` for its own spine build; with that patched to `None`
    too, `_commit_via_head_spine` returns `None` ("unreadable HEAD tree
    spine"), and `commit_authored_new_file` fails loud rather than falling
    through to a spawning ladder that would run the receiver's hooks (AC4's
    decline arm).

    Cold-cache dependency: `git_state.head_blobs` memoises its result in a
    process-wide `_HEAD_BLOBS_CACHE` keyed on `(repo, head_sha, paths)`. This
    test's spawn count and its `run_git`-among-origins assertion depend on a
    COLD cache entry for that key -- guaranteed here because each test gets
    a fresh `tmp_path` repo and this is the first read of it, matching the
    same dependency `test_commit_authored_new_file.py` documents beside its
    own identity-pin comment. A future fixture that reuses a repo or reads
    `head_blobs` before this counter starts would silently turn the spawn
    into a cache hit and drop `run_git` from the recorded origins.
    """
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "head-spine-unreadable-topic")

    monkeypatch.setattr(git_native, "read_tree_spine", lambda *a, **kw: None)
    monkeypatch.setattr(git_state, "read_tree_spine", lambda *a, **kw: None)

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _memo_send(
            {"dry_run": False, "topic": "head-spine-unreadable-topic"}, repo_root=sender_repo
        )

    assert result["exit_code"] != 0, result
    assert result["acted"] == []
    assert not (
        sender_repo / ".coordinator-local" / "memo-outbox" / "sent" / "head-spine-unreadable-topic.md"
    ).exists()
    inbox_files = [
        p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
        if p.name != ".gitkeep"
    ]
    assert inbox_files == []

    budget = _budget()
    assert len(spawns) == budget["head_spine_unreadable_refused"], [s.origin for s in spawns]
    origins = [s.origin for s in spawns]
    assert "unattributed" not in origins, origins
    assert "run_git" in origins, origins


def test_receiver_signing_enabled_spawn_count_matches_budget_and_reaches_write_signed_commit_object(
    tmp_path, monkeypatch
):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "true"],
        cwd=str(receiver_repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )
    claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "receiver-signing-topic")

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _memo_send(
            {"dry_run": False, "topic": "receiver-signing-topic"}, repo_root=sender_repo
        )

    assert result["exit_code"] == 0, result
    budget = _budget()
    assert len(spawns) == budget["receiver_signing_enabled"], [s.origin for s in spawns]
    origins = [s.origin for s in spawns]
    assert "unattributed" not in origins, origins
    assert "write_signed_commit_object" in origins, origins
