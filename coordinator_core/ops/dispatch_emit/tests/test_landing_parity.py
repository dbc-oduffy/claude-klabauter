"""Parity suite for the run landing: each invariant of docs/reference/run-landing-invariants.md that a
behavioural test can pin, run against the old path (`dispatch.terminal_commit` handler) and the rebuilt
`run_landing.land_run`.

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

IMPLS = ["old", "new"]

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


# --- C3b: post-landing bookkeeping (RL-07d, RL-08, RL-11, RL-12) ---------------------------------
#
# RL-07d trailers and the RL-08 records fold into the ONE landing commit. RL-11 (coded stamp) and RL-12
# (register / ledger writeback) cannot: each writes the landing's own sha into a plan or sizing file,
# and a commit cannot contain the hash of itself, so both ride one follow-up commit built after it.
# The old path lands plan-run receipts and an ask run's tracked outcome in a "(process files)" commit
# between the two, so a case asserts those files are tracked at HEAD for both and in the landing commit
# for the new path only. The repo fixtures carry `archive/` so both paths treat the repo as onboarded.

SIZING = "state/sizings/2026-10-09-x.yaml"
PLAN = "docs/plans/p.md"
DLV = "dlv-alpha-aaaaaa"
_SID = "11111111-2222-3333-4444-555555555555"
_SIZING_TEXT = """schema: sizing-object
intent: "x"
estimate:
  tshirt: S
  provisional: true
route: spec-dispatch
detents: []
fork: null
xl_exit: null
status: routed
premise:
  provenance: read
  evidence: e
requirement_register:
  sources:
  - {doc: brief.md, ref: r@abc}
  rows:
  - {id: r1, source_text: t r1, surface: ui, status: open, claimed_by: [docs/plans/p.md], wired: false}
"""
_PLAN_TEXT = (
    f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n- id: C2\n  title: t2\n  disposition: open\n"
    "  body: |\n    b\n```\n"
)


@pytest.fixture
def plan_repo(tmp_path, monkeypatch):
    """A repo whose committed plan (rows C1, C2 open) cites a committed sizing carrying a register row."""
    from coordinator_core.ops.dispatch_emit import completion_receipt as cr

    root = _repo(tmp_path, monkeypatch)
    (root / "archive").mkdir()
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / SIZING).write_text(_SIZING_TEXT, encoding="utf-8")
    (root / PLAN).write_text(_PLAN_TEXT, encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "plan seed"], root)
    monkeypatch.setattr(cr, "_actual_loe", lambda r, s: "S")
    monkeypatch.setattr("coordinator_core.session.claims.list_claims_by_session_checked",
                        lambda sid, cwd=None: ([], []))
    return root


def _plan_fire(impl: str, root: Path, chunks=None, review=None, **extra) -> dict:
    return _fire(impl, root, chunks or {"C1": ["a.py"]}, inline_review=review,
                 request_kwargs={"plan_path": PLAN, "deliverable_id": DLV}, session_id=_SID, **extra)


def _trailer(root: Path, key: str, rev: str = "HEAD") -> list:
    out = _git(["show", "-s", f"--format=%(trailers:key={key},valueonly=true)", rev], root)
    return [ln for ln in out.splitlines() if ln.strip()]


def _plan_at(root: Path, rev: str) -> str:
    return _git(["show", f"{rev}:{PLAN}"], root)


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07d_the_commit_carries_a_resolves_sizing_trailer_for_the_cited_sizing(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _plan_fire(impl, plan_repo)

    assert out["committed"] is True, out
    assert _trailer(plan_repo, "Resolves-Sizing", out["sha"]) == ["2026-10-09-x S"]


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07d_an_incomplete_run_carries_no_resolves_sizing_trailer(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _plan_fire(impl, plan_repo, {"C1": ["a.py"], "C2": ["b.py"]}, incomplete_chunks=["C2"])

    assert out["committed"] is True, out
    assert _trailer(plan_repo, "Resolves-Sizing", out["sha"]) == []


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07d_a_review_stage_edit_outside_the_declared_writes_lands_with_a_review_residue_trailer(
        impl, plan_repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edits

    stray = "elsewhere/tests/test_stray.py"
    (plan_repo / "elsewhere" / "tests").mkdir(parents=True)
    (plan_repo / stray).write_text("review_edit = 1\n", encoding="utf-8")
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {stray: "agent-1"})
    review = {**reviewed(), "reverify": {"edited": [stray, "elsewhere/tests/gone.py"]}}

    out = _plan_fire(impl, plan_repo, review=review)

    assert out["committed"] is True, out
    assert out["review_residue"] == [stray]
    assert _trailer(plan_repo, "Review-Residue", out["sha"]) == [stray]
    assert stray in _landed_names_at(plan_repo, out["sha"])


def _landed_names_at(root: Path, rev: str) -> set:
    return set(_git(["show", "--name-only", "--format=", rev], root).split())


@pytest.mark.parametrize("impl", IMPLS)
def test_rl08_a_plan_runs_completion_receipt_rides_the_landing_commit(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _plan_fire(impl, plan_repo)

    assert out["committed"] is True, out
    assert len(out["receipts"]) == 1
    receipt = out["receipts"][0]
    assert _git(["ls-tree", "--name-only", "HEAD", "--", receipt], plan_repo) == receipt
    if impl == "new":
        assert receipt in _landed_names_at(plan_repo, out["sha"])


@pytest.mark.parametrize("impl", IMPLS)
def test_rl11_the_landed_row_flips_coded_at_the_landing_sha_in_a_follow_up_commit(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _plan_fire(impl, plan_repo)

    assert out["committed"] is True, out
    assert out["rows_coded"] == {PLAN: ["C1"]}
    assert _git(["rev-parse", "HEAD"], plan_repo) == out["coded_sha"]
    _git(["merge-base", "--is-ancestor", out["sha"], out["coded_sha"]], plan_repo)
    if impl == "new":
        assert _git(["rev-parse", "HEAD~1"], plan_repo) == out["sha"]
    stamped = _plan_at(plan_repo, "HEAD")
    assert f"disposition_ref: {out['sha']}" in stamped
    assert "- id: C2\n  title: t2\n  disposition: open" in stamped.replace("\r\n", "\n")
    assert PLAN not in _landed_names_at(plan_repo, out["sha"])


@pytest.mark.parametrize("impl", IMPLS)
def test_rl11_a_done_chunk_with_no_changed_path_closes_coded_with_the_no_change_detail(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (plan_repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(["add", "b.py"], plan_repo)
    _git(["commit", "-q", "-m", "b already landed"], plan_repo)

    out = _plan_fire(impl, plan_repo, {"C1": ["a.py"], "C2": ["b.py"]})

    assert out["committed"] is True, out
    assert out["rows_coded"] == {PLAN: ["C1", "C2"]}
    from coordinator_core.ops.dispatch_emit.plan_row_coding import NOOP_DETAIL

    folded = _plan_at(plan_repo, "HEAD").replace("\r\n", "\n").replace("\n    ", " ")
    if impl == "new":
        assert NOOP_DETAIL.replace("'", "''") in folded
    else:
        # RL-49 strikes the old path's outside-landing archaeology: it names the commit that carried
        # the row's writes, where the new path closes the row with the no-change detail.
        assert "landed outside this run" in folded


@pytest.mark.parametrize("impl", IMPLS)
def test_rl11_an_incomplete_chunks_row_stays_open(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _plan_fire(impl, plan_repo, {"C1": ["a.py"], "C2": ["b.py"]}, incomplete_chunks=["C2"])

    assert out["committed"] is True, out
    assert out["rows_coded"] == {PLAN: ["C1"]}


@pytest.mark.parametrize("impl", IMPLS)
def test_rl12_the_judged_register_row_is_written_back_at_the_landing_sha_in_the_follow_up_commit(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    row = {"id": "r1", "claim": "this-plan", "status": "met", "wired": True, "surface": "ui",
           "observed_ref": "x.py::f"}
    review = {**reviewed(), "criterion": {"status": "met", "observation": "ok", "register_rows": [row]}}

    out = _plan_fire(impl, plan_repo, review=review)

    assert out["committed"] is True and "register_writeback_error" not in out, out
    import yaml

    register = yaml.safe_load(_git(["show", f"HEAD:{SIZING}"], plan_repo))["requirement_register"]
    assert register["rows"][0]["status"] == "partial" and register["rows"][0]["last_progress_at"]
    assert register["rollup"]["partial"] == 1
    assert SIZING not in _landed_names_at(plan_repo, out["sha"])
    assert SIZING in _landed_names_at(plan_repo, out["coded_sha"])
    assert _git(["status", "--porcelain", "--untracked-files=no"], plan_repo) == ""


@pytest.mark.parametrize("impl", IMPLS)
def test_rl12_the_resolution_row_names_the_landing_sha(impl, plan_repo):
    (plan_repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _plan_fire(impl, plan_repo)

    assert out["committed"] is True, out
    rows = (plan_repo / ".coordinator-local" / "sizings.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(r)["resolved_sha"] for r in rows] == [out["sha"]]


@pytest.fixture
def ask_repo(tmp_path, monkeypatch):
    """A repo with one declared ask run (manifest, no halted peer) for the RL-08 record files."""
    from coordinator_core.ops.dispatch_emit import completion_receipt as cr

    root = _repo(tmp_path, monkeypatch)
    (root / "archive").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(cr, "_actual_loe", lambda r, s: "S")
    monkeypatch.setattr("coordinator_core.session.claims.list_claims_by_session_checked",
                        lambda sid, cwd=None: ([], []))
    session_core.init(SELF, cwd=str(root))
    (root / "docs" / "plans").mkdir(parents=True)
    (root / PLAN).write_text(_PLAN_TEXT.replace(f"sizing_object: {SIZING}\n", ""), encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "plan seed"], root)
    return root


def _ask_fire(impl: str, root: Path, ledger: bool = False) -> dict:
    """Declare the ask run and land it. The write baseline is captured before `a.py` exists, so the
    landing sees the run's own change; `ledger` binds the committed sizing and passes a met verdict."""
    extra: dict = {}
    if ledger:
        (root / "state" / "sizings").mkdir(parents=True)
        (root / SIZING).write_text(_SIZING_TEXT, encoding="utf-8")
        _git(["add", SIZING], root)
        _git(["commit", "-q", "-m", "sizing seed"], root)
        extra["ledger_verdict"] = {"items": [
            {"id": "r1", "status": "met", "wired": True, "command": "c", "observed": "o"}]}
    rdir = _declare(root, RUN_B, wb.capture(root, ["a.py"]))
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    if ledger:
        (rdir / "run.json").write_text(
            json.dumps({"run_id": RUN_B, "sizing_rel": SIZING, "all_of_pm_ask": False}), encoding="utf-8")
    manifest = json.loads((rdir / "manifest.json").read_text(encoding="utf-8"))
    manifest["rows"][0]["writes"] = ["a.py"]
    (rdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),),
                            plan_path=PLAN, deliverable_id=DLV)
    marker = root / RUN_DIR_ROOT / RUN_B / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}state/warp/{RUN_B}/manifest.json\n", encoding="utf-8")
    return _land(impl, root, {
        "inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN_B, **extra,
    })


@pytest.mark.parametrize("impl", IMPLS)
def test_rl08_an_ask_runs_tracked_outcome_and_delivery_note_ride_the_landing_commit(impl, ask_repo):
    from coordinator_core.ops.dispatch_emit.ask_contract import delivery_note_rel

    note = delivery_note_rel(RUN_B)
    (ask_repo / note).parent.mkdir(parents=True, exist_ok=True)
    (ask_repo / note).write_text("# delivered\n", encoding="utf-8")

    out = _ask_fire(impl, ask_repo)

    assert out["committed"] is True, out
    outcome = f"state/run-evidence/{RUN_B}/outcome.json"
    assert note in _landed_names_at(ask_repo, out["sha"])
    assert json.loads(_git(["show", f"HEAD:{outcome}"], ask_repo))["state"] == "landed"
    if impl == "new":
        assert outcome in _landed_names_at(ask_repo, out["sha"])


@pytest.mark.parametrize("impl", IMPLS)
def test_rl08_an_ask_runs_local_receipt_is_named_by_a_completion_receipt_trailer(impl, ask_repo):
    out = _ask_fire(impl, ask_repo)

    assert out["committed"] is True, out
    named = _trailer(ask_repo, "Completion-Receipt", out["sha"])
    assert len(out["receipts"]) == 1 and named == [Path(out["receipts"][0]).stem]
    assert out["receipts"][0] not in _landed_names_at(ask_repo, out["sha"])


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07d_an_ask_run_with_a_ledger_carries_a_ledger_receipt_trailer(impl, ask_repo):
    out = _ask_fire(impl, ask_repo, ledger=True)

    assert out["committed"] is True, out
    assert _trailer(ask_repo, "Ledger-Receipt", out["sha"]) == ["review: 1 verdicts"]


@pytest.mark.parametrize("impl", IMPLS)
def test_rl07d_an_ask_run_without_a_ledger_carries_no_ledger_receipt_trailer(impl, ask_repo):
    out = _ask_fire(impl, ask_repo)

    assert out["committed"] is True, out
    assert _trailer(ask_repo, "Ledger-Receipt", out["sha"]) == []


@pytest.mark.parametrize("impl", IMPLS)
def test_rl12_a_passed_ledger_verdict_is_written_to_the_bound_register_at_the_landing_sha(impl, ask_repo):
    import yaml

    out = _ask_fire(impl, ask_repo, ledger=True)

    assert out["committed"] is True and "requirements_writeback_error" not in out, out
    row = yaml.safe_load(_git(["show", f"HEAD:{SIZING}"], ask_repo))["requirement_register"]["rows"][0]
    assert row["status"] == "met" and row["met_by"] == [f"{RUN_B}@{out['sha']}"]
    assert SIZING not in _landed_names_at(ask_repo, out["sha"])
    assert _git(["status", "--porcelain", "--untracked-files=no"], ask_repo) == ""
