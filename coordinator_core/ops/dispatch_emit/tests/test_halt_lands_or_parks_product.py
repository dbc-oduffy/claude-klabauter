"""``dispatch.land_run`` halted mode parks the run's own halt-record-only product in the halted record:
the tree is clean for it, ``evicted.json`` indexes the bytes, a path the run's baseline does not name
stays put, and a refire restores the parked bytes."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_evict, land_run_halt
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-cccccc"
REC = f"{RECORD_DIR_ROOT}/{RUN}"
DONE_PY = "pkg/done_chunk.py"
OPEN_PY = "pkg/open_chunk.py"
UNNAMED = "pkg/unnamed.py"
IDLE = "pkg/idle.md"
LEDGER = {"halted": "ledger-check", "reason": "row C2 open"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _dirty(root: Path) -> set:
    return {line[3:] for line in _git(root, "status", "--porcelain", "-uall").splitlines()}


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "README.md", "seed\n")
    _write(tmp_path, DONE_PY, "VALUE = 0\n")
    _write(tmp_path, IDLE, "idle\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _stage_run(root: Path) -> dict:
    """A finished chunk C1 (``DONE_PY`` edited, ``IDLE`` untouched, ``UNNAMED`` outside the baseline)
    and an unfinished C2 whose ``OPEN_PY`` is recorded only in ``measured-rev.json``."""
    _write(root, "reports/C1.md", "DONE: C1\n")
    _write(root, "reports/C2.md", "PARTIAL: C2\n")
    chunks = (
        ChunkCommit(id="C1", title="C1", paths=(DONE_PY, IDLE, UNNAMED), report="reports/C1.md"),
        ChunkCommit(id="C2", title="C2", paths=(OPEN_PY,), report="reports/C2.md"),
    )
    _write(root, "run.mjs", render_marker(CommitRequest(chunks=chunks, session_id=None, repo_root=None, plan_path=None)) + "\n")
    _write(root, f"{REC}/ask.md", "ask\n")
    _write(root, f"{REC}/run.json", json.dumps({}))
    baseline = {
        DONE_PY: _git(root, "rev-parse", f"HEAD:{DONE_PY}").strip(),
        IDLE: _git(root, "rev-parse", f"HEAD:{IDLE}").strip(),
        OPEN_PY: None,
    }
    _write(root, f"{REC}/manifest.json", json.dumps({"rows": [], "write_baseline": baseline}))
    _write(root, f"{REC}/measured-rev.json", json.dumps({"paths": [OPEN_PY]}))
    _write(root, DONE_PY, "VALUE = 1\n")
    _write(root, OPEN_PY, "OPEN = 1\n")
    _write(root, UNNAMED, "UNNAMED = 1\n")
    return {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: LEDGER, "incomplete_chunks": ["C2"]}


def test_halted_land_parks_the_runs_product_and_leaves_no_run_dirt(repo):
    params = _stage_run(repo)
    unnamed_bytes = (repo / UNNAMED).read_bytes()

    out = land_run_halt.land(repo, repo, params)

    assert out["committed"] is True, out
    assert set(out["evicted"]) == {DONE_PY, OPEN_PY}
    assert (repo / OPEN_PY).read_text(encoding="utf-8") == "OPEN = 1\n"
    assert (repo / DONE_PY).read_text(encoding="utf-8") == "VALUE = 1\n"
    dirty = _dirty(repo)
    assert {DONE_PY, OPEN_PY} <= dirty and IDLE not in dirty, dirty
    index = json.loads((repo / REC / halt_tree_evict.EVICTED_FILE).read_text(encoding="utf-8"))
    assert set(index) == {DONE_PY, OPEN_PY}
    for entry in index.values():
        assert (repo / REC / halt_tree_evict.BLOBS_DIR / entry["blob"]).is_file()
    landed = _git(repo, "show", "--name-only", "--format=", out["sha"]).split()
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in landed
    assert not {DONE_PY, OPEN_PY, UNNAMED} & set(landed), landed
    assert out["withheld"][DONE_PY] == "evicted-parked"
    assert (repo / UNNAMED).read_bytes() == unnamed_bytes and UNNAMED in dirty
    assert out["withheld"][UNNAMED] == land_run_halt.HALT_RECORD_ONLY


def test_a_write_equal_to_the_baseline_parks_nothing(repo):
    params = _stage_run(repo)

    out = land_run_halt.land(repo, repo, params)

    assert IDLE not in out["evicted"]
    assert (repo / IDLE).read_text(encoding="utf-8") == "idle\n"


def test_a_peer_held_product_path_is_not_parked(repo, monkeypatch):
    params = _stage_run(repo)
    peer_bytes = (repo / DONE_PY).read_bytes()
    from coordinator_core.ops.ceremony import commit_v2

    monkeypatch.setattr(
        commit_v2, "_peer_claim_warnings",
        lambda root, paths, holds, own_sid=None: holds.append((DONE_PY, "peer-sid")) or [],
    )

    out = land_run_halt.land(repo, repo, params)

    assert out["withheld"][DONE_PY] == land_run_halt.PEER_HELD
    assert DONE_PY not in out.get("evicted", [])
    assert (repo / DONE_PY).read_bytes() == peer_bytes


def test_a_refire_finds_the_parked_product_already_in_the_tree(repo):
    params = _stage_run(repo)
    before = {p: (repo / p).read_bytes() for p in (DONE_PY, OPEN_PY)}
    land_run_halt.land(repo, repo, params)

    result = halt_tree_evict.restore(repo, repo / REC, owned_paths=[DONE_PY, OPEN_PY])

    assert result == {"restored": [], "drift": {}, "refused": {}}
    assert {p: (repo / p).read_bytes() for p in (DONE_PY, OPEN_PY)} == before
