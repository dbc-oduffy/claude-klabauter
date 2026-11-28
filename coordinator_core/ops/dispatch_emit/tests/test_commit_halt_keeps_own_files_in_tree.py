"""A commit halt records the run's bytes in the halted record and leaves them in the working tree: through
`land_run_halt.land` and through `halted_record_commit.commit_halted_record` at default arguments, one own
`.py` and one own `.md` hold the run's bytes after the halt, the record blob exists, `evicted.json` names the
blob as `head`, and a refire's `restore_own_delta` has nothing to write."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_evict, halted_record_commit, land_run_halt
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-dddddd"
REC = f"{RECORD_DIR_ROOT}/{RUN}"
OWN_PY = "pkg/own_chunk.py"
OWN_MD = "notes/own_note.md"
LEDGER = {"halted": "ledger-check", "reason": "row C2 open"}
RUN_BYTES = {OWN_PY: b"VALUE = 1\n", OWN_MD: b"head note\nrun line\n"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, data: bytes) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "README.md", b"seed\n")
    _write(tmp_path, OWN_PY, b"VALUE = 0\n")
    _write(tmp_path, OWN_MD, b"head note\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _stage_run(root: Path) -> dict:
    """Chunk C1 finished (both own paths edited), chunk C2 unfinished, so the halt withholds both paths."""
    _write(root, "reports/C1.md", b"DONE: C1\n")
    _write(root, "reports/C2.md", b"PARTIAL: C2\n")
    chunks = (
        ChunkCommit(id="C1", title="C1", paths=(OWN_PY, OWN_MD), report="reports/C1.md"),
        ChunkCommit(id="C2", title="C2", paths=("pkg/open_chunk.py",), report="reports/C2.md"),
    )
    marker = render_marker(CommitRequest(chunks=chunks, session_id=None, repo_root=None, plan_path=None))
    _write(root, "run.mjs", (marker + "\n").encode("utf-8"))
    _write(root, f"{REC}/ask.md", b"ask\n")
    _write(root, f"{REC}/run.json", json.dumps({}).encode("utf-8"))
    baseline = {p: _git(root, "rev-parse", f"HEAD:{p}").strip() for p in (OWN_PY, OWN_MD)}
    baseline["pkg/open_chunk.py"] = None
    _write(root, f"{REC}/manifest.json", json.dumps({"rows": [], "write_baseline": baseline}).encode("utf-8"))
    _write(root, f"{REC}/measured-rev.json", json.dumps({"paths": ["pkg/open_chunk.py"]}).encode("utf-8"))
    for rel, data in RUN_BYTES.items():
        _write(root, rel, data)
    _write(root, "pkg/open_chunk.py", b"OPEN = 1\n")
    return {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: LEDGER, "incomplete_chunks": ["C2"]}


def _assert_kept_in_tree(root: Path) -> None:
    for rel, data in RUN_BYTES.items():
        assert (root / rel).read_bytes() == data, f"{rel} was reset away from the run's bytes"
    index = json.loads((root / REC / halt_tree_evict.EVICTED_FILE).read_text(encoding="utf-8"))
    assert {OWN_PY, OWN_MD} <= set(index)
    for rel, data in RUN_BYTES.items():
        entry = index[rel]
        assert entry["blob"] == blob_sha(data)
        assert (root / REC / halt_tree_evict.BLOBS_DIR / entry["blob"]).read_bytes() == data
        assert entry["head"] == entry["blob"], f"{rel}: evicted.json head is not the kept blob"
    restored = halt_tree_evict.restore_own_delta(root, root / REC, RUN)
    assert not [p for p, r in restored.items() if r.get("status") == "restored"], restored
    for rel, data in RUN_BYTES.items():
        assert (root / rel).read_bytes() == data


def test_land_run_halt_keeps_the_runs_own_py_and_md_in_the_tree(repo):
    params = _stage_run(repo)

    out = land_run_halt.land(repo, repo, params)

    assert out["committed"] is True, out
    _assert_kept_in_tree(repo)


def test_halted_record_commit_keeps_the_runs_own_py_and_md_in_the_tree(repo):
    params = _stage_run(repo)

    out = halted_record_commit.commit_halted_record(repo, repo, params)

    assert out["committed"] is True, out
    _assert_kept_in_tree(repo)
