"""halt_closure.open_chunks against real throwaway git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import import_closure
from coordinator_core.ops.dispatch_emit.halt_closure import open_chunks, partial_chunks
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/tracked.py", "def f():\n    pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_untracked_sibling_import_opens_chunk(repo):
    _write(repo, "pkg/untracked.py", "y = 1\n")
    _write(repo, "pkg/a.py", "import pkg.untracked\n")
    got = open_chunks(repo, {"pkg/a.py": "A"}, ["pkg/a.py"], [import_closure])
    assert list(got) == ["A"]
    assert "pkg/untracked.py is uncommitted in the worktree" in got["A"][0]


def test_tracked_import_stays_closed(repo):
    _write(repo, "pkg/a.py", "from pkg.tracked import f\n")
    assert open_chunks(repo, {"pkg/a.py": "A"}, ["pkg/a.py"], [import_closure]) == {}


def test_transitive_chunk_opens_once_its_definer_opens(repo):
    _write(repo, "pkg/untracked.py", "y = 1\n")
    _write(repo, "pkg/a.py", "import pkg.untracked\n")
    _write(repo, "pkg/newmod.py", "z = 1\n")
    _write(repo, "pkg/b.py", "import pkg.newmod\n")
    # newmod belongs to the same chunk as a.py, so b lands only while A does.
    owner = {"pkg/a.py": "A", "pkg/newmod.py": "A", "pkg/b.py": "B"}
    got = open_chunks(repo, owner, list(owner), [import_closure])
    assert sorted(got) == ["A", "B"]


def test_chunk_importing_landing_definer_stays_closed_while_definer_lands(repo):
    _write(repo, "pkg/newmod.py", "z = 1\n")
    _write(repo, "pkg/b.py", "import pkg.newmod\n")
    owner = {"pkg/newmod.py": "A", "pkg/b.py": "B"}
    assert open_chunks(repo, owner, list(owner), [import_closure]) == {}


def test_raising_detector_opens_its_candidate_chunks(repo):
    class Boom:
        NAME = "boom"

        def candidates(self, gate_paths):
            return [p for p in gate_paths if p.endswith("a.py")]

        def detect(self, root, gate_paths, head):
            raise RuntimeError("nope")

    _write(repo, "pkg/a.py", "x = 1\n")
    _write(repo, "pkg/c.py", "x = 2\n")
    owner = {"pkg/a.py": "A", "pkg/c.py": "C"}
    got = open_chunks(repo, owner, list(owner), [Boom()])
    assert list(got) == ["A"]
    assert "raised RuntimeError" in got["A"][0]


def test_candidates_raising_detector_opens_every_landing_chunk(repo):
    class Boom:
        NAME = "boom"

        def candidates(self, gate_paths):
            raise RuntimeError("nope")

    _write(repo, "pkg/a.py", "x = 1\n")
    _write(repo, "pkg/c.py", "x = 2\n")
    owner = {"pkg/a.py": "A", "pkg/c.py": "C"}
    got = open_chunks(repo, owner, list(owner), [Boom()])
    assert sorted(got) == ["A", "C"]


def test_empty_landing_returns_empty(repo):
    assert open_chunks(repo, {"pkg/a.py": "A"}, [], [import_closure]) == {}


def test_partial_chunk_names_missing_paths_and_whole_chunks_pass():
    owner = {"a1": "A", "a2": "A", "b1": "B", "run/x": "C"}
    got = partial_chunks(owner, ["a1", "a2", "b1"], ["a1", "b1"])
    assert got == {"A": ["a2"]}
    assert partial_chunks(owner, ["a1", "a2", "b1"], ["a1", "a2", "b1"]) == {}
    assert partial_chunks(owner, ["unowned"], []) == {}
    assert partial_chunks(owner, ["a1", "a2"], []) == {"A": ["a1", "a2"]}


def test_halt_commit_withholds_chunk_with_a_peer_held_write(repo, monkeypatch):
    import json

    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc

    _write(repo, "pkg/a1.py", "x = 1\n")
    _write(repo, "pkg/a2.py", "x = 2\n")
    _write(repo, "pkg/b1.py", "x = 3\n")
    chunks = [
        type("C", (), {"id": "A", "paths": ["pkg/a1.py", "pkg/a2.py"], "report": None})(),
        type("C", (), {"id": "B", "paths": ["pkg/b1.py"], "report": None})(),
    ]
    monkeypatch.setattr(hrc, "done_chunks", lambda *_a: chunks)
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.terminal_commit._own_prefix_files", lambda *_a: []
    )

    def claims(root, paths, holds, own_sid=None):
        holds.append(("pkg/a2.py", "peer"))
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", claims)
    seen: dict = {}

    def fake_dispatch(op, params, repo_root=None):
        seen.update(params)
        return {"committed": True}

    monkeypatch.setattr("coordinator_core.warm.entry_seam.reentrant_dispatch", fake_dispatch)
    (repo / "state/warp/r1").mkdir(parents=True)
    halted = {"halted": "ledger-check"}
    reply = hrc._land(
        repo, repo, {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}},
        "r1", "state/warp/r1", halted,
    )
    assert "pkg/a1.py" not in seen.get("paths", [])
    assert "pkg/a2.py" not in seen.get("paths", [])
    assert "pkg/b1.py" in seen["paths"]
    assert reply["withheld"]["pkg/a1.py"] == "chunk-write-set-partial"
    assert reply["withheld"]["pkg/a2.py"] == "peer-held"
    assert reply["landed_chunks"] == ["B"]
    assert "Lands-Chunks: B" in seen["message"]
    json.dumps(reply)


def _attribute_read_shape(root: Path) -> None:
    """Tracked caller reads `li.old()`; worktree adds `li.new_fn()` with `new_fn` only in the dirty definer."""
    _write(root, "pkg/li.py", "def old():\n    return 1\n")
    _write(
        root, "pkg/caller.py",
        "def run():\n    from pkg import li\n    return li.old()\n",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed li and caller")
    _write(root, "pkg/li.py", "def old():\n    return 1\n\n\ndef new_fn():\n    return 2\n")
    _write(
        root, "pkg/caller.py",
        "def run():\n    from pkg import li\n    return li.old() + li.new_fn()\n",
    )


def test_attribute_read_of_a_withheld_definer_opens_the_caller_chunk(repo):
    _attribute_read_shape(repo)
    got = open_chunks(
        repo, {"pkg/caller.py": "C4", "pkg/li.py": "C1"}, ["pkg/caller.py"], (import_closure,)
    )
    assert list(got) == ["C4"]
    assert "new_fn" in got["C4"][0]
    assert "pkg/li.py" in got["C4"][0]


def test_attribute_read_stays_closed_when_the_definer_lands_with_it(repo):
    _attribute_read_shape(repo)
    owner = {"pkg/caller.py": "C4", "pkg/li.py": "C1"}
    assert open_chunks(repo, owner, ["pkg/caller.py", "pkg/li.py"], (import_closure,)) == {}


def test_halt_commit_withholds_caller_chunk_whose_attribute_definer_is_peer_held(repo, monkeypatch):
    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc

    _attribute_read_shape(repo)
    _write(repo, "pkg/other.py", "x = 3\n")
    chunks = [
        type("C", (), {"id": "C1", "paths": ["pkg/li.py"], "report": None})(),
        type("C", (), {"id": "C4", "paths": ["pkg/caller.py"], "report": None})(),
        type("C", (), {"id": "C9", "paths": ["pkg/other.py"], "report": None})(),
    ]
    monkeypatch.setattr(hrc, "done_chunks", lambda *_a: chunks)
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.terminal_commit._own_prefix_files", lambda *_a: []
    )

    def claims(root, paths, holds, own_sid=None):
        holds.append(("pkg/li.py", "peer"))
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", claims)
    seen: dict = {}

    def fake_dispatch(op, params, repo_root=None):
        seen.update(params)
        return {"committed": True}

    monkeypatch.setattr("coordinator_core.warm.entry_seam.reentrant_dispatch", fake_dispatch)
    (repo / "state/warp/r1").mkdir(parents=True)
    reply = hrc._land(
        repo, repo, {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}},
        "r1", "state/warp/r1", {"halted": "ledger-check"},
    )
    assert reply["withheld"]["pkg/li.py"] == "peer-held"
    assert reply["withheld"]["pkg/caller.py"] == "import-closure-open"
    assert "pkg/caller.py" not in seen.get("paths", [])
    assert "pkg/li.py" not in seen.get("paths", [])
    assert "pkg/other.py" in seen["paths"]


def test_finding_on_non_owner_path_is_ignored(repo):
    _write(repo, "pkg/untracked.py", "y = 1\n")
    _write(repo, "pkg/a.py", "import pkg.untracked\n")
    assert open_chunks(repo, {}, ["pkg/a.py"], [import_closure]) == {}
