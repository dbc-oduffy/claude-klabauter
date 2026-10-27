"""leak_gate and attribution_gate at a many-path shape: work scales with the paths they judge."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import authoring_leaks
from coordinator_core.authoring_leaks import import_closure, leak_gate
from coordinator_core.ops.ceremony import commit_gates
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

N = 2000
N_PY = 2


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.example"], r)
    _git(["config", "user.name", "t"], r)
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], r)
    _git(["commit", "-q", "-m", "seed"], r)
    return r


def _populate(r: Path) -> list:
    paths = []
    d = r / "data"
    d.mkdir()
    for i in range(N - N_PY):
        (d / f"f{i}.json").write_text("{}\n", encoding="utf-8")
        paths.append(f"data/f{i}.json")
    for i in range(N_PY):
        (d / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
        paths.append(f"data/m{i}.py")
    return paths


def test_import_closure_detect_sees_only_py_paths(repo, monkeypatch):
    paths = _populate(repo)
    seen = []
    real = import_closure.detect

    def spy(root, gate_paths, head, **k):
        seen.append(list(gate_paths))
        return real(root, gate_paths, head, **k)

    monkeypatch.setattr(import_closure, "detect", spy)
    outcome = leak_gate(repo, paths, detectors=[import_closure])
    assert outcome.passed
    assert len(seen) == 1
    assert sorted(seen[0]) == ["data/m0.py", "data/m1.py"]


def test_unlisted_detector_still_sees_every_path(repo):
    paths = _populate(repo)
    seen = []

    class Det:
        NAME = "custom"

        def candidates(self, gate_paths):
            return []

        def detect(self, root, gate_paths, head):
            seen.append(len(gate_paths))
            return []

    assert leak_gate(repo, paths, detectors=[Det()]).passed
    assert seen == [N]
    assert "custom" not in authoring_leaks.JUDGED_SUFFIXES


def test_attribution_gate_reads_each_path_once_and_skips_undecodable(repo, monkeypatch):
    paths = _populate(repo)
    (repo / "data" / "blob.bin").write_bytes(b"\xff\xfe\x00\x80" * 8)
    paths.append("data/blob.bin")
    reads = {}
    real_read = Path.read_text

    def counting(self, *a, **k):
        reads[self.name] = reads.get(self.name, 0) + 1
        return real_read(self, *a, **k)

    spine_paths = []
    real_spine = commit_gates.read_tree_spine

    def spine(root, p):
        spine_paths.extend(p)
        return real_spine(root, p)

    monkeypatch.setattr(Path, "read_text", counting)
    monkeypatch.setattr(commit_gates, "read_tree_spine", spine)
    outcome = commit_gates.attribution_gate(repo, paths, vacated=[])
    assert outcome.passed
    assert max(reads[Path(p).name] for p in paths) == 1
    assert "data/blob.bin" not in spine_paths


def test_attribution_gate_still_refuses_an_added_attribution_line(repo):
    paths = _populate(repo)
    (repo / "data" / "m0.py").write_text("# Review: the Staff Engineer (Finding 1)\n", encoding="utf-8")
    outcome = commit_gates.attribution_gate(repo, paths, vacated=[])
    assert not outcome.passed
    assert any("data/m0.py" in d for d in outcome.diagnostics)
