"""Parity suite for the run landing: each invariant of docs/reference/run-landing-invariants.md that a
behavioural test can pin, run against the old path (`dispatch.terminal_commit` handler) and the rebuilt
`run_landing.land_run`. The `new` leg is xfail(strict) until `land_run` has a body, so an unexpected
pass on `new` fails the suite and forces the marker off.

Test names carry the register id they pin (`test_rlNN_...`).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import run_landing, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import REFUSED_UNREVIEWED
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

IMPLS = [
    "old",
    pytest.param(
        "new",
        marks=pytest.mark.xfail(
            strict=True, raises=NotImplementedError, reason="run_landing.land_run has no body until C3"
        ),
    ),
]

SELF = "5e1f0000-0000-4000-8000-0000000000f1"
X = "pkg/shared.py"
LINES = 120
RUN_A = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 600))}-aaaaaa"
RUN_B = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-bbbbbb"
A_LINE = 5
B_LINE = 115


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return _repo(tmp_path, monkeypatch)


def _land(impl: str, root: Path, params: dict) -> dict:
    if impl == "old":
        return terminal_commit._handler(params, repo_root=root / ".git")
    return run_landing.land_run(params, root / ".git")


def _fire(impl: str, root: Path, chunks, inline_review=None, request_kwargs=None, **extra) -> dict:
    """Write a request marker declaring `chunks` ({id: paths}) and land it."""
    request = CommitRequest(
        chunks=tuple(ChunkCommit(id=cid, title=f"t{cid}", paths=tuple(paths)) for cid, paths in chunks.items()),
        **(request_kwargs or {}),
    )
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {
        "inline_review": reviewed() if inline_review is None else inline_review,
        "script_path": "run.mjs",
        "incomplete_chunks": [],
        **extra,
    }
    return _land(impl, root, params)


def _head(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root)


def _message(root: Path) -> str:
    return _git(["log", "-1", "--format=%B"], root)


def _landed_names(root: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def _review(slice_files: list) -> dict:
    out = dict(reviewed())
    out["prep"] = {"product_files": len(slice_files), "slice_files": slice_files}
    return out


# --- RL-01 / RL-02 / RL-05: a shared path a halted peer run also dirtied --------------------------


def _body(a_edit: bool = False, b_edit: bool = False) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    if a_edit:
        rows[A_LINE - 1] = f"x_{A_LINE} = 'A-EDIT'"
    if b_edit:
        rows[B_LINE - 1] = f"x_{B_LINE} = 'B-EDIT'"
    return "\n".join(rows) + "\n"


def _crlf(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("utf-8")


def _declare(root: Path, run_id: str, baseline: dict) -> Path:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    if run_id == RUN_A:
        (rdir / "run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run_id}",
        rows=(ManifestRow(
            id="C1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md",
            writes=(X,), wave=0,
        ),),
        review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt",
        write_baseline=baseline,
    )
    (rdir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    return rdir


@pytest.fixture
def shared_repo(tmp_path, monkeypatch):
    """Halted run A and live run B both declare X; the worktree X carries A's edit and B's edit."""
    root = _repo(tmp_path, monkeypatch)
    _git(["config", "core.autocrlf", "true"], root)
    (root / "pkg").mkdir()
    (root / X).write_bytes(_crlf(_body()))
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "shared seed"], root)
    seed = _head(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    rdir = _declare(root, RUN_A, wb.capture(root, [X]))
    (rdir / "halted.json").write_text(
        json.dumps({
            "status": "halted", "run_base_sha": seed,
            "run_artifacts": {"withheld": {X: "halt-record-only"}},
        }),
        encoding="utf-8",
    )
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=_crlf(_body(a_edit=True)),
        capture_output=True, check=True, **no_console_creationflags(),
    ).stdout.decode().strip()
    (rdir / "kept.json").write_text(
        json.dumps({"chunks": {"C1": {"paths": {X: blob}}}, "plan_gate": None}), encoding="utf-8"
    )
    (root / X).write_bytes(_crlf(_body(a_edit=True)))
    _declare(root, RUN_B, wb.capture(root, [X]))
    run_token.mint(root, RUN_B, "run.mjs", session_id=SELF)
    (root / X).write_bytes(_crlf(_body(a_edit=True, b_edit=True)))
    return root


def _fire_b(impl: str, root: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(X,)),))
    marker = root / RUN_DIR_ROOT / RUN_B / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}state/warp/{RUN_B}/manifest.json\n", encoding="utf-8")
    return _land(impl, root, {
        "inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN_B,
    })


@pytest.mark.parametrize("impl", IMPLS)
def test_rl01_only_the_runs_own_hunks_of_a_shared_path_land(impl, shared_repo):
    seed = _head(shared_repo)

    out = _fire_b(impl, shared_repo)

    assert out["committed"] is True, out
    assert _head(shared_repo) != seed
    landed = _git(["show", f"HEAD:{X}"], shared_repo).replace(chr(13) + chr(10), chr(10))
    assert f"x_{B_LINE} = 'B-EDIT'" in landed
    assert "A-EDIT" not in landed


@pytest.mark.parametrize("impl", IMPLS)
def test_rl02_a_foreign_hunk_is_left_dirty_in_the_worktree_and_out_of_the_commit(impl, shared_repo):
    out = _fire_b(impl, shared_repo)

    assert out["committed"] is True, out
    assert f"x_{A_LINE} = 'A-EDIT'" in (shared_repo / X).read_bytes().decode("utf-8")
    still_dirty = _git(["diff", "HEAD", "--", X], shared_repo)
    assert "A-EDIT" in still_dirty
    assert "B-EDIT" not in still_dirty


@pytest.mark.parametrize("impl", IMPLS)
def test_rl05_no_index_only_landing_the_index_matches_the_committed_tree(impl, shared_repo):
    out = _fire_b(impl, shared_repo)

    assert out["committed"] is True, out
    assert _git(["diff", "--cached", "--name-only"], shared_repo) == ""


@pytest.mark.parametrize("impl", IMPLS)
def test_rl05_a_landed_path_leaves_worktree_index_and_head_in_agreement(impl, repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py"]})

    assert out["committed"] is True, out
    assert _git(["status", "--porcelain", "--", "a.py"], repo) == ""
    assert _git(["show", "HEAD:a.py"], repo) == "a = 1"


# --- RL-03: ccc7d1, a staged path in no reviewed slice -------------------------------------------


@pytest.mark.parametrize("impl", IMPLS)
def test_rl03_a_staged_path_in_no_reviewed_slice_refuses_and_commits_nothing(impl, repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    head = _head(repo)

    out = _fire(impl, repo, {"C1": ["a.py", "b.py"]}, inline_review=_review(["a.py"]))

    assert out["committed"] is False
    assert out["refused"] == REFUSED_UNREVIEWED
    assert out["unreviewed_paths"] == ["b.py"]
    assert _head(repo) == head


@pytest.mark.parametrize("impl", IMPLS)
def test_rl03_a_fully_covered_staged_set_lands(impl, repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py", "b.py"]}, inline_review=_review(["a.py", "./b.py"]))

    assert out["committed"] is True, out
    assert _landed_names(repo) >= {"a.py", "b.py"}


# --- RL-04: pathspec scoping ---------------------------------------------------------------------


@pytest.mark.parametrize("impl", IMPLS)
def test_rl04_a_glob_character_in_a_declared_directory_is_matched_literally(impl, repo):
    declared = repo / "dir[x]"
    other = repo / "dirx"
    declared.mkdir()
    other.mkdir()
    (declared / "a.py").write_text("a = 1\n", encoding="utf-8")
    (declared / "c.py").write_text("c = 1\n", encoding="utf-8")
    (other / "b.py").write_text("b = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["dir[x]/a.py"]})

    assert out["committed"] is True, out
    assert _landed_names(repo) == {"dir[x]/a.py"}
    undeclared = out.get("undeclared_dirty") or []
    assert "dir[x]/c.py" in undeclared
    assert "dirx/b.py" not in undeclared
    assert "dirx/b.py" in _git(["status", "--porcelain", "--untracked-files=all"], repo)


# --- RL-06 / RL-07: commit-message lines ---------------------------------------------------------


@pytest.mark.parametrize("impl", IMPLS)
def test_rl06_the_commit_carries_the_co_authored_by_line(impl, repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "claude")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py"]})

    assert out["committed"] is True, out
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in _message(repo).splitlines()


@pytest.mark.parametrize("impl", IMPLS)
def test_rl06_an_unknown_provider_adds_no_co_authored_by_line(impl, repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "not-a-provider")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py"]})

    assert out["committed"] is True, out
    assert "Co-Authored-By" not in _message(repo)


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07_the_commit_carries_the_inline_review_stamp_line(impl, repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py"]}, inline_review=reviewed(slices=3, fixes=2))

    assert out["committed"] is True, out
    stamp = _git(["show", "-s", "--format=%(trailers:key=Inline-Review,valueonly=true)", "HEAD"], repo)
    assert "applies rev-stem -- execute-review: 3 slices, 2 fixes" in stamp


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07_the_commit_carries_the_deliverable_id_line(impl, repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py"]}, request_kwargs={"deliverable_id": "dlv-plan111"})

    assert out["committed"] is True, out
    ids = _git(["show", "-s", "--format=%(trailers:key=Deliverable-Id,valueonly=true)", "HEAD"], repo)
    assert "dlv-plan111" in ids.split()


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07_the_commit_names_the_run_base_in_a_checkpoint_run_closed_line(impl, repo):
    base = _head(repo)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py"]}, run_base_sha=base)

    assert out["committed"] is True, out
    closed = _git(["show", "-s", "--format=%(trailers:key=Checkpoint-Run-Closed,valueonly=true)", "HEAD"], repo)
    assert closed.split() == [base]


# --- RL-13: no landing after the dispatcher gave up ----------------------------------------------


@pytest.mark.parametrize("impl", IMPLS)
def test_rl13_a_landing_after_the_dispatch_deadline_moves_no_ref(impl, repo, monkeypatch):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    head = _head(repo)
    monkeypatch.setattr(dispatch_abandon, "is_abandoned", lambda: True)

    out = _fire(impl, repo, {"C1": ["a.py"]})

    assert out.get("committed") is not True, out
    assert _head(repo) == head


# --- RL-15 / RL-16: the two refusals DR-474 keeps ------------------------------------------------


@pytest.mark.parametrize("impl", IMPLS)
def test_rl15_a_credential_added_by_the_landing_refuses(impl, repo):
    key = "-----BEGIN " + "RSA PRIVATE KEY-----"
    (repo / "creds.py").write_text(f'KEY = """{key}"""\n', encoding="utf-8")
    head = _head(repo)

    out = _fire(impl, repo, {"C1": ["creds.py"]})

    assert out["committed"] is False
    assert out["refused"] == "secret-in-diff"
    assert _head(repo) == head


@pytest.mark.parametrize("impl", IMPLS)
def test_rl16_a_declared_deletion_lands_as_a_deletion(impl, repo):
    (repo / "gone.py").write_text("g = 1\n", encoding="utf-8")
    _git(["add", "gone.py"], repo)
    _git(["commit", "-q", "-m", "add gone"], repo)
    (repo / "gone.py").unlink()

    out = _fire(impl, repo, {"C1": ["gone.py"]})

    assert out["committed"] is True, out
    assert _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split() == ["seed.txt"]
    assert _git(["status", "--porcelain", "--untracked-files=no"], repo) == ""


# --- RL-10 / RL-09: the review floor and the stranded set ----------------------------------------


@pytest.mark.parametrize("impl", IMPLS)
def test_rl10_a_run_with_no_review_output_refuses_unreviewed(impl, repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    head = _head(repo)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")

    out = _land(impl, repo, {"script_path": "run.mjs", "incomplete_chunks": []})

    assert out["committed"] is False
    assert out["refused"] == "unreviewed"
    assert _head(repo) == head


@pytest.mark.parametrize("impl", IMPLS)
def test_rl09_an_incomplete_chunks_paths_stay_uncommitted_and_are_named_stranded(impl, repo):
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")

    out = _fire(impl, repo, {"C1": ["a.py"], "C2": ["b.py"]}, incomplete_chunks=["C2"])

    assert out["committed"] is True, out
    assert _landed_names(repo) == {"a.py"}
    assert out["stranded"] == {"C2": ["b.py"]}
    assert "b.py" in _git(["status", "--porcelain", "--untracked-files=all"], repo)
