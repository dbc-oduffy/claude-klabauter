"""Two runs in one tree: run A's terminal commit lands only its own claimed paths. A path an
unlanded sibling run B claims (live token or halted) that reaches A through an admission arm (here
the docs-stage `doc_paths`) is withheld and named under `sibling_withheld`, B's file stays dirty,
and an overlap on B's path never refuses A; a path A itself declares still lands."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c2"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
B_DOC = "docs/b.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _run_id(suffix: str) -> str:
    stamp = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ask-{stamp}-{suffix}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, "README.md", "seed\n")
    _write(root, B_DOC, "x\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    _write(root, "src/a.py", "a\n")
    session_scope.touch(SELF, "src/a.py", cwd=str(root))
    _write(root, B_DOC, "x\nB edit\n")
    session_scope.touch(SELF, B_DOC, cwd=str(root))
    return root


def _sibling_b(repo: Path, *, halted: bool) -> None:
    run_id = _run_id("bbbbbb")
    _write(repo, f"state/warp/{run_id}/manifest.json",
           json.dumps({"rows": [{"id": "C1", "writes": [B_DOC]}], "doc_writes": []}))
    if halted:
        _write(repo, f"state/warp/{run_id}/halted.json", json.dumps(
            {"status": "halted", "run_artifacts": {"withheld": {B_DOC: "claimed"}}}))
    else:
        run_token.mint(repo, run_id, "b.mjs", session_id=SELF)


def _run_a(repo: Path, *, own_paths=("src/a.py",), **extra) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(own_paths)),))
    (repo / "run-a.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    params = {
        "script_path": "run-a.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
        "session_id": SELF, "run_id": _run_id("aaaaaa"), "doc_paths": [B_DOC],
    }
    params.update(extra)
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


@pytest.mark.parametrize("halted", [False, True], ids=["live-token", "halted"])
def test_a_siblings_claimed_path_admitted_by_an_arm_is_withheld_and_stays_dirty(repo, halted):
    _sibling_b(repo, halted=halted)
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py"]
    assert list(out["sibling_withheld"]) == [B_DOC]
    assert out["sibling_withheld"][B_DOC].endswith("-bbbbbb")
    assert (repo / B_DOC).read_text(encoding="utf-8") == "x\nB edit\n"
    assert B_DOC in _git(["status", "--porcelain"], repo)


def test_control_without_a_sibling_the_admitted_doc_path_lands(repo):
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert B_DOC in _committed(repo)
    assert "sibling_withheld" not in out


def test_a_path_the_run_declares_lands_even_when_a_sibling_claims_it(repo):
    token = run_token.mint(repo, _run_id("aaaaaa"), "run-a.mjs", session_id=SELF)
    assert run_token.redeem(repo, _run_id("aaaaaa"), token, "run-a.mjs")
    _sibling_b(repo, halted=False)
    session_scope.touch(SELF, B_DOC, cwd=str(repo))
    out = _run_a(repo, own_paths=("src/a.py", B_DOC))
    assert out["committed"] is True, out
    assert _committed(repo) == [B_DOC, "src/a.py"]
    assert "sibling_withheld" not in out


def test_an_overlap_on_a_siblings_path_does_not_refuse_the_run(repo):
    base = _git(["rev-parse", "HEAD"], repo).strip()
    foreign = repo / B_DOC
    mine = foreign.read_text(encoding="utf-8")
    foreign.write_text("x\nforeign line\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "foreign adds a line"], repo)
    foreign.write_text(mine, encoding="utf-8")
    _sibling_b(repo, halted=False)
    out = _run_a(repo, run_base_sha=base)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py"]
    assert B_DOC in out["sibling_withheld"]


def test_an_overlap_on_the_runs_own_path_still_refuses_with_the_reland_remedy(repo):
    base = _git(["rev-parse", "HEAD"], repo).strip()
    own = repo / "README.md"
    own.write_text("seed\nforeign line\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "foreign adds a line"], repo)
    own.write_text("seed\n", encoding="utf-8")
    session_scope.touch(SELF, "README.md", cwd=str(repo))
    out = _run_a(repo, own_paths=("src/a.py", "README.md"), run_base_sha=base, doc_paths=[])
    assert out.get("refused") == "since-base-overlap", out
    assert "re-land: Workflow(" in out["error"] and "dispatch.ask_gate" not in out["error"]
    assert B_DOC not in out["error"]
