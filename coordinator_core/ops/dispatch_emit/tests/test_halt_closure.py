"""halt_closure.open_chunks against real throwaway git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import import_closure
from coordinator_core.ops.dispatch_emit.halt_closure import open_chunks
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


def test_finding_on_non_owner_path_is_ignored(repo):
    _write(repo, "pkg/untracked.py", "y = 1\n")
    _write(repo, "pkg/a.py", "import pkg.untracked\n")
    assert open_chunks(repo, {}, ["pkg/a.py"], [import_closure]) == {}
