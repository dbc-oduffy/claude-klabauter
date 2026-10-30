"""End to end: a run that changes a public symbol closes through ``close_background.run_background``
with the default runners, and the symbol-knowledge leg leaves the definition current in the sidecar.

Real git repo, real SCIP-shaped symtab under a tmp settings home, real close-job record. Only the
detached spawn (run synchronously after the job returns, as the detached process would) and the
agent runner are injected.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.symbol_knowledge import agent_batches, flag, sidecar

pytestmark = pytest.mark.spawns_process

RUN = "run-e2e"
PARENT_SRC = "def alpha():\n    return 1\n\n\ndef _hidden():\n    return 2\n"
PREFIX = "scip-python python demo 0.1 `pkg.mod`/"
ALPHA = PREFIX + "alpha()."
HIDDEN = PREFIX + "_hidden()."
LEG = "symbol-knowledge"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _span(src: str, needle: str, end_marker: str) -> "tuple[int, int]":
    start = src.index(needle)
    end = src.index(end_marker, start) + len(end_marker)
    return len(src[:start].encode()), len(src[:end].encode())


def _symtab(graph_dir: Path) -> Path:
    graph_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(graph_dir / flag.SYMTAB_NAME)
    db.execute(
        "CREATE TABLE symtab (scip_symbol TEXT PRIMARY KEY, symbol_id TEXT NOT NULL, file TEXT NOT NULL,"
        " byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX idx_symtab_file ON symtab(file)")
    for sym, needle, end in ((ALPHA, "def alpha", "return 1"), (HIDDEN, "def _hidden", "return 2")):
        start, stop = _span(PARENT_SRC, needle, end)
        db.execute("INSERT INTO symtab VALUES (?,?,?,?,?,?,?)", (sym, sym, "pkg/mod.py", start, stop, "function", sym))
    db.commit()
    db.close()
    (graph_dir / "graph.db").write_bytes(b"")
    return graph_dir / "graph.db"


class _Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.spawned: "list[list[str]]" = []
        self.prompts: "list[tuple[str, str]]" = []

    def sha_after(self, new_src: str) -> str:
        (self.root / "pkg" / "mod.py").write_text(new_src, encoding="utf-8", newline="\n")
        _git(self.root, "commit", "-qam", "change")
        return _git(self.root, "rev-parse", "HEAD")

    def queue(self, sha: str, **inputs) -> None:
        run_close.write_close_job(
            self.root, RUN, (LEG,), datetime.now().astimezone() + timedelta(minutes=10),
            terminal_sha=sha, inputs=inputs or None,
        )

    def record(self) -> dict:
        return json.loads((self.root / run_close.close_job_path(RUN)).read_text(encoding="utf-8"))

    def run(self, sha: str) -> dict:
        ctx = cb.JobContext(self.root, self.root / ".git", RUN, (), None, terminal_sha=sha)
        outcomes = cb.run_background(ctx)
        for argv in self.spawned:
            assert agent_batches.main(argv) == 0
        return outcomes


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    root = tmp_path / "proj-x"
    (root / "pkg").mkdir(parents=True)
    (root / "archive").mkdir()
    _git(root, "init", "-q")
    (root / "pkg" / "mod.py").write_text(PARENT_SRC, encoding="utf-8", newline="\n")
    (root / ".gitignore").write_text(".coordinator-local/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "parent")
    settings = tmp_path / "settings"
    _symtab(settings / "project-rag" / "indexes" / "proj_x" / "root")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    for var, val in (("NAME", "t"), ("EMAIL", "t@t")):
        monkeypatch.setenv(f"GIT_AUTHOR_{var}", val)
        monkeypatch.setenv(f"GIT_COMMITTER_{var}", val)
    e = _Env(root)

    def fake_spawn(_root, _script, args):
        e.spawned.append(list(args))
        return True

    def fake_runner(model, prompt, *, cwd=None):
        e.prompts.append((model, prompt))
        return json.dumps({ALPHA: "alpha returns the new value."})

    monkeypatch.setattr("coordinator_core.ops.ceremony.detached_spawn.spawn_detached", fake_spawn)
    monkeypatch.setattr(agent_batches, "claude_runner", fake_runner)
    return e


def test_public_symbol_change_leaves_a_current_definition_and_the_record_says_ran(env):
    sha = env.sha_after(PARENT_SRC.replace("return 1", "return 11"))
    env.queue(sha)

    outcomes = env.run(sha)

    assert outcomes[LEG].status == "queued"
    assert env.record()["legs"][LEG]["status"] == "ran"
    assert env.record()["legs"][LEG]["detail"].startswith("1 current at ")
    entries = sidecar.read_sidecar(env.root)
    assert list(entries) == [ALPHA]
    assert entries[ALPHA]["sha"] == sha
    assert entries[ALPHA]["definition"] == "alpha returns the new value."
    assert [m for m, _ in env.prompts] == ["sonnet"]
    tracked = _git(env.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert sidecar.SIDECAR_REL in tracked
    assert _git(env.root, "show", "--name-only", "--format=", "HEAD").splitlines() == [sidecar.SIDECAR_REL]


def test_second_run_over_a_known_symbol_goes_to_haiku_adjust(env):
    first = env.sha_after(PARENT_SRC.replace("return 1", "return 11"))
    env.queue(first)
    env.run(first)
    env.spawned.clear()
    env.prompts.clear()

    second = env.sha_after(PARENT_SRC.replace("return 1", "return 12"))
    env.queue(second)
    env.run(second)

    assert [m for m, _ in env.prompts] == ["haiku"]
    assert sidecar.read_sidecar(env.root)[ALPHA]["sha"] == second


def test_private_symbol_change_is_not_applicable_and_launches_no_agent(env):
    sha = env.sha_after(PARENT_SRC.replace("return 2", "return 22"))
    env.queue(sha)

    outcomes = env.run(sha)

    assert outcomes[LEG].status == "not-applicable"
    assert env.record()["legs"][LEG]["status"] == "not-applicable"
    assert env.spawned == [] and env.prompts == []
    assert not sidecar.sidecar_path(env.root).exists()


def test_halt_touching_the_flagged_symbol_leaves_one_trap_line(env):
    sha = env.sha_after(PARENT_SRC.replace("return 1", "return 11"))
    env.queue(sha, unlanded=["C9"], unlanded_paths={"C9": ["pkg/mod.py"]})

    env.run(sha)

    traps = sidecar.read_sidecar(env.root)[ALPHA]["traps"]
    assert len(traps) == 1
    assert "halt" in traps[0] and "pkg/mod.py" in traps[0]


def test_flag_step_is_within_the_process_time_and_spawn_budget(env):
    from coordinator_core.benchmarks import symbol_knowledge_flag as bench
    from coordinator_core.symbol_knowledge.leg import index_graph_db

    sha = env.sha_after(PARENT_SRC.replace("return 1", "return 11"))
    graph_db = index_graph_db(env.root)
    assert graph_db is not None

    ms, spawns, flagged = bench.measure(env.root / ".git", sha, graph_db, runs=5)

    assert flagged == 1
    assert spawns == 0
    assert ms <= bench.BAR_MS
