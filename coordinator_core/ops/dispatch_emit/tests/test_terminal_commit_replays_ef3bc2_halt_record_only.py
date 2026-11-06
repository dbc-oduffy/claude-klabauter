"""Replay of run ef3bc2's refire: C1 blocked, C2/C3 kept by the halted record, every kept path left
dirty as halt-record-only. The terminal commit credits the kept chunks and lands them instead of
refusing no-credited-chunk-landed; a chunk the record does not keep still refuses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, kept_work, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, TERMINAL_RESUME_LEDGER_PARAM
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000e3"
HALTED_RUN = "ask-20261105T215350-ef3bc2"
PATHS = {"C1": "pkg/c1.py", "C2": "pkg/c2.py", "C3": "pkg/c3.py"}
DOCS = "docs/run.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, body: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(body, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _shape(root: Path, kept: tuple) -> None:
    """Dirty C1-C3 product, a refire marker with C1-C3 and a docs chunk (no reports: the kept rows were
    not re-executed), and the halted record keeping ``kept`` with halt-record-only withholds."""
    for rel in PATHS.values():
        _write(root, rel, "X = 1\n")
    _write(root, DOCS, "What shipped.\n")
    chunks = [ChunkCommit(id=c, title=c, paths=(p,)) for c, p in PATHS.items()]
    chunks.append(ChunkCommit(id="docs", title="docs", paths=(DOCS,)))
    _write(root, "run.mjs", "// emitted\n" + render_marker(CommitRequest(chunks=tuple(chunks))) + "\n")
    record = root / RECORD_DIR_ROOT / HALTED_RUN
    halted_run.write_halt_marker(record, halted="ledger-check", sizing_rel=None)
    halted_run.record_withheld(record, {PATHS[c]: "halt-record-only" for c in kept})
    kept_work.write(
        record,
        {c: {"paths": {PATHS[c]: kept_work.fingerprint(root, PATHS[c])}} for c in kept},
        None,
    )


def _call(root: Path) -> dict:
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": ["C1", "C2", "C3"],
            "inline_review": reviewed(), "session_id": SELF,
            TERMINAL_RESUME_LEDGER_PARAM: {"run_id": HALTED_RUN, "blocking": [], "unlanded": []},
        },
        repo_root=root / ".git",
    )


def _landed(root: Path) -> set:
    return set(_git(["ls-files"], root).split())


def test_kept_delivered_chunks_are_credited_and_landed(repo):
    _shape(repo, kept=("C2", "C3"))
    out = _call(repo)
    assert out["committed"] is True, out
    assert out.get("refused") != "no-credited-chunk-landed"
    assert {"C2", "C3"} <= set(out["chunks_committed"])
    assert {PATHS["C2"], PATHS["C3"]} <= _landed(repo)
    assert PATHS["C1"] not in _landed(repo)
    assert out["stranded"] == {"C1": [PATHS["C1"]]}


def test_no_kept_chunk_still_refuses(repo):
    _shape(repo, kept=())
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo)
    assert out["committed"] is False, out
    assert out["refused"] == "no-credited-chunk-landed"
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert not ({PATHS["C2"], PATHS["C3"]} & _landed(repo))


def test_kept_chunk_whose_bytes_drifted_is_not_credited(repo):
    _shape(repo, kept=("C2", "C3"))
    _write(repo, PATHS["C2"], "X = 2\n")
    _write(repo, PATHS["C3"], "X = 3\n")
    out = _call(repo)
    assert out["committed"] is False, out
    assert out["refused"] == "no-credited-chunk-landed"


def test_note_naming_a_kept_row_bars_the_credit(repo):
    _shape(repo, kept=("C2", "C3"))
    (repo / RECORD_DIR_ROOT / HALTED_RUN / "notes.jsonl").write_text(
        json.dumps({"note": "C2 and C3 must be redone", "at": "t"}) + "\n", encoding="utf-8"
    )
    out = _call(repo)
    assert out["committed"] is False, out
    assert out["refused"] == "no-credited-chunk-landed"


def test_kept_entry_covering_only_part_of_a_chunks_paths_is_not_credited(repo):
    _shape(repo, kept=("C2", "C3"))
    extra = "pkg/c2_extra.py"
    _write(repo, extra, "Y = 1\n")
    chunks = [ChunkCommit(id="C1", title="C1", paths=(PATHS["C1"],)),
              ChunkCommit(id="C2", title="C2", paths=(PATHS["C2"], extra)),
              ChunkCommit(id="C3", title="C3", paths=(PATHS["C3"],)),
              ChunkCommit(id="docs", title="docs", paths=(DOCS,))]
    _write(repo, "run.mjs", "// emitted\n" + render_marker(CommitRequest(chunks=tuple(chunks))) + "\n")
    out = _call(repo)
    assert out["committed"] is True, out
    assert PATHS["C3"] in _landed(repo)
    assert not ({PATHS["C2"], extra} & _landed(repo))
    assert "C2" not in out["chunks_committed"]
