"""`dispatch.land_blobs` through its registered handler: declared blobs land in process with no git spawn, and a
hunk outside the run's recorded set refuses the landing naming the foreign run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_blobs, land_blobs_contract as contract
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

X = "pkg/shared.py"
OWN_RUN = "ask-20261114T100000-aaaaaa"
PEER_RUN = "ask-20261114T100001-bbbbbb"
LINES = 120


def _git(args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          **no_console_creationflags()).stdout.strip()


def _body(**edits: str) -> bytes:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    for line, text in edits.items():
        rows[int(line[1:]) - 1] = text
    return ("\n".join(rows) + "\n").encode("utf-8")


def _blob(root: Path, data: bytes) -> str:
    return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=data, capture_output=True,
                          check=True, **no_console_creationflags()).stdout.decode().strip()


def _record(root: Path, run_id: str, path: str, sha: str) -> None:
    rdir = root / RECORD_DIR_ROOT / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {path: sha}}}}), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "pkg").mkdir()
    (root / X).write_bytes(_body())
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(root: Path, paths=(X,), **extra) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),))
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"script_path": "run.mjs", "run_id": OWN_RUN, "inline_review": reviewed(), "incomplete_chunks": [],
              **extra}
    return land_blobs._handler(params, repo_root=root / ".git")


class _Spawns:
    def __init__(self, monkeypatch) -> None:
        self.argvs: list = []
        original = subprocess.Popen.__init__
        spawns = self

        def spy(popen, args, *a, **kw):
            spawns.argvs.append(args)
            original(popen, args, *a, **kw)

        monkeypatch.setattr(subprocess.Popen, "__init__", spy)


def test_declared_blobs_land_in_one_commit_with_no_git_spawn(repo, monkeypatch):
    (repo / X).write_bytes(_body(l5="x_5 = 'OWN'"))
    head = _git(["rev-parse", "HEAD"], repo)
    spawns = _Spawns(monkeypatch)

    out = _fire(repo)

    assert spawns.argvs == []
    assert out["committed"] is True, out
    assert out["landed_paths"] == [X]
    assert _git(["rev-parse", "HEAD"], repo) == out["sha"] != head
    assert _git(["rev-parse", f"HEAD:{X}"], repo) == _git(["hash-object", X], repo)
    assert _git(["status", "--porcelain", "--", X], repo) == ""


def test_a_hunk_outside_the_runs_recorded_set_refuses_naming_the_foreign_run(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    own = _body(l115="x_115 = 'OWN'")
    _record(repo, OWN_RUN, X, _blob(repo, own))
    _record(repo, PEER_RUN, X, _blob(repo, _body(l5="x_5 = 'PEER'")))
    (repo / X).write_bytes(_body(l5="x_5 = 'PEER'", l115="x_115 = 'OWN'"))

    out = _fire(repo)

    assert out["committed"] is False
    assert out["refused"] == contract.FOREIGN_HUNK
    assert out["foreign_paths"] == [X]
    assert out["foreign_runs"] == [PEER_RUN]
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert PEER_RUN in out["error"]


def test_the_runs_own_recorded_blob_lands(repo):
    own = _body(l115="x_115 = 'OWN'")
    _record(repo, OWN_RUN, X, _blob(repo, own))
    (repo / X).write_bytes(own)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert _git(["show", f"HEAD:{X}"], repo).splitlines()[114] == "x_115 = 'OWN'"


def test_a_peer_hunk_in_an_unrecorded_path_refuses_and_a_disjoint_edit_lands(repo):
    _record(repo, PEER_RUN, X, _blob(repo, _body(l5="x_5 = 'PEER'")))
    (repo / X).write_bytes(_body(l5="x_5 = 'PEER'", l115="x_115 = 'OWN'"))

    refused = _fire(repo)

    assert refused["refused"] == contract.FOREIGN_HUNK
    assert refused["foreign_runs"] == [PEER_RUN]

    (repo / X).write_bytes(_body(l115="x_115 = 'OWN'"))

    landed = _fire(repo)

    assert landed["committed"] is True, landed


def test_a_raw_crlf_recorded_blob_matches_the_checkin_form_under_autocrlf(repo):
    _git(["config", "core.autocrlf", "true"], repo)
    own = _body(l115="x_115 = 'OWN'").replace(b"\n", b"\r\n")
    _record(repo, OWN_RUN, X, _blob(repo, own))
    (repo / X).write_bytes(own)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert b"\r" not in subprocess.run(["git", "show", f"HEAD:{X}"], cwd=str(repo), capture_output=True,
                                      check=True, **no_console_creationflags()).stdout


def test_a_run_with_no_review_output_refuses_unreviewed(repo):
    (repo / X).write_bytes(_body(l5="x_5 = 'OWN'"))

    out = _fire(repo, inline_review=None)

    assert out["refused"] == contract.UNREVIEWED


def test_a_record_mode_call_refuses_mode_ambiguous(repo):
    out = _fire(repo, halted_by={"halted": "x"})

    assert out["refused"] == contract.MODE_AMBIGUOUS


def test_a_review_with_an_unreviewed_slice_still_lands_and_reports_the_gap(repo):
    (repo / X).write_bytes(_body(l5="x_5 = 'OWN'"))
    review = reviewed(prep={"slice_files": ["other.py"]})

    out = _fire(repo, inline_review=review)

    assert out["committed"] is True
    assert out["unreviewed_paths"] == [X]


def test_the_op_is_registered_and_scoped_under_the_temporary_name():
    from coordinator_core.op_scopes import OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP[contract.TEMP_OP_NAME] == "coordinator_core.ops.dispatch_emit.land_blobs"
    assert OP_KEY_SCOPE[contract.TEMP_OP_NAME] == "common_dir"
    assert contract.ENTRY == "coordinator_core.ops.dispatch_emit.land_blobs:land"
