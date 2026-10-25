"""Both halt-record writers record kept work and land kept.json with the halt commit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import kept_work, land_run_halt, row_returns, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261015T000000-aaaaaa"
KEPT_REL = f"{RECORD_DIR_ROOT}/{RUN}/{kept_work.KEPT_FILE}"
PLAN = "docs/plans/p.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "seed.txt", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    for name in ("ask.md", "run.json"):
        _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/{name}", f"{name}\n")
    return tmp_path


def _chunks(root: Path, chunks: dict, reports: dict) -> None:
    built = []
    for cid, paths in chunks.items():
        _write(root, f"reports/{cid}.md", reports[cid])
        for p in paths:
            _write(root, p, f"{p} = 1\n")
        built.append(ChunkCommit(id=cid, title=cid, paths=tuple(paths), report=f"reports/{cid}.md"))
    marker = render_marker(CommitRequest(chunks=tuple(built), session_id=None, repo_root=None, plan_path=None))
    _write(root, "run.mjs", f"{marker}\n")


def _halted(with_plan: bool) -> dict:
    halted: dict = {"halted": "refusal", "reason": "readiness refused"}
    if with_plan:
        halted["plan"] = PLAN
        halted["blitz"] = {"pulled": [{"planPath": PLAN, "reviews": "r.json", "coverage": "c"}]}
    return halted


def _params(halted: dict, consumed: object) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted}
    if consumed is not None:
        params[row_returns.CONSUMED_RETURNS_PARAM] = consumed
    return params


def _terminal(root: Path, halted: dict, consumed: object = None) -> dict:
    return terminal_commit._handler(_params(halted, consumed), repo_root=root / ".git")


def _land_run(root: Path, halted: dict, consumed: object = None) -> dict:
    return land_run_halt.land(root, root, _params(halted, consumed))


WRITERS = pytest.mark.parametrize("call", [_terminal, _land_run], ids=["halted_record_commit", "land_run_halt"])


def _head_names(root: Path) -> list:
    return _git(root, "show", "--name-only", "--format=", "HEAD").split()


@WRITERS
def test_done_chunk_is_recorded_and_kept_json_lands_with_the_record(repo, call):
    _chunks(repo, {"C1": ["a.py", "b.py"]}, {"C1": "DONE: C1\n"})

    out = call(repo, _halted(False))

    assert out["committed"] is True, out
    assert KEPT_REL in _head_names(repo)
    assert "a.py" not in _head_names(repo) and "b.py" not in _head_names(repo)
    kept = json.loads(_git(repo, "show", f"HEAD:{KEPT_REL}"))
    assert set(kept["chunks"]["C1"]["paths"]) == {"a.py", "b.py"}
    assert kept["chunks"]["C1"]["paths"]["a.py"] == kept_work.fingerprint(repo, "a.py")
    assert kept["plan_gate"] is None
    assert out["kept"] == {"chunks": ["C1"], "plan_gate": False}


@WRITERS
def test_kept_chunk_blob_survives_the_worktree_file_being_overwritten(repo, call):
    _chunks(repo, {"C1": ["a.py"]}, {"C1": "DONE: C1\n"})
    want = kept_work.fingerprint(repo, "a.py")

    out = call(repo, _halted(False))

    _write(repo, "a.py", "overwritten\n")
    assert _git(repo, "cat-file", "blob", want) == "a.py = 1\n"
    assert "kept_unpersisted" not in out


def test_kept_chunk_path_that_cannot_be_persisted_is_named(repo, tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc

    _chunks(repo, {"C1": ["a.py"]}, {"C1": "DONE: C1\n"})
    monkeypatch.setattr(hrc, "_persist_blobs", lambda root, common_dir, post: set())

    out = _terminal(repo, _halted(False))

    assert out["kept_unpersisted"] == ["a.py"]
    assert out["kept"]["chunks"] == ["C1"]


@WRITERS
def test_unfinished_chunk_is_never_recorded(repo, call):
    _chunks(repo, {"C1": ["a.py"], "C2": ["h.py"]}, {"C1": "DONE: C1\n", "C2": "PARTIAL: C2\n"})

    out = call(repo, _halted(False))

    assert out["kept"]["chunks"] == ["C1"]
    kept = json.loads(_git(repo, "show", f"HEAD:{KEPT_REL}"))
    assert list(kept["chunks"]) == ["C1"]


@WRITERS
def test_refusal_plan_gate_is_recorded_with_the_plan_bytes(repo, call):
    _write(repo, PLAN, "plan\n")
    _git(repo, "add", PLAN)
    _git(repo, "commit", "-q", "-m", "plan")

    out = call(repo, _halted(True))

    assert out["committed"] is True, out
    kept = json.loads(_git(repo, "show", f"HEAD:{KEPT_REL}"))
    assert kept["chunks"] == {}
    assert kept["plan_gate"]["plan"] == PLAN
    assert kept["plan_gate"]["reviews"] == "r.json"
    assert out["kept"] == {"chunks": [], "plan_gate": True}


@WRITERS
def test_nothing_kept_writes_no_file_and_no_reply_key(repo, call):
    out = call(repo, _halted(False))

    assert out["committed"] is True, out
    assert KEPT_REL not in _head_names(repo)
    assert "kept" not in out
    assert not (repo / KEPT_REL).exists()


RETURNS_REL = f"{RECORD_DIR_ROOT}/{RUN}/{row_returns.RETURNS_FILE}"


@WRITERS
def test_consumed_return_lands_and_writes_empty_done_row_is_kept(repo, call):
    _chunks(repo, {"C1": ["a.py"], "C2": []}, {"C1": "DONE: C1\n", "C2": "DONE: C2\n"})

    out = call(repo, _halted(False), {"C2": "DONE: figures 960449 B", "C9": 7})

    assert out["committed"] is True, out
    assert RETURNS_REL in _head_names(repo) and KEPT_REL in _head_names(repo)
    stored = json.loads(_git(repo, "show", f"HEAD:{RETURNS_REL}"))
    assert list(stored) == ["C2"] and stored["C2"]["text"] == "DONE: figures 960449 B"
    kept = json.loads(_git(repo, "show", f"HEAD:{KEPT_REL}"))
    assert kept["chunks"]["C2"] == {"paths": {}, "return": stored["C2"]["sha256"]}
    assert set(kept["chunks"]["C1"]["paths"]) == {"a.py"}
    assert out["returns"] == ["C2"]


@WRITERS
def test_unconsumed_reply_is_never_written(repo, call):
    _chunks(repo, {"C1": ["a.py"]}, {"C1": "DONE: C1\n"})

    out = call(repo, _halted(False), {})

    assert RETURNS_REL not in _head_names(repo)
    assert "returns" not in out
    assert out["kept"]["chunks"] == ["C1"]


@WRITERS
def test_kept_row_carries_the_prior_runs_entry(repo, call):
    prior = "ask-20261014T000000-bbbbbb"
    row_returns.write(repo / RECORD_DIR_ROOT / prior, {"C2": "DONE: figures 960449 B"})
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps({"rows": [{"id": "C2", "kept_from": prior}]}))
    _chunks(repo, {"C1": ["a.py"], "C2": []}, {"C1": "DONE: C1\n", "C2": "DONE: C2\n"})

    call(repo, _halted(False), {"C2": "DONE: C2 kept from prior (report on disk)"})

    stored = json.loads(_git(repo, "show", f"HEAD:{RETURNS_REL}"))
    assert stored["C2"]["text"] == "DONE: figures 960449 B"
