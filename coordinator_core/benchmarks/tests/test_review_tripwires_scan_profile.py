"""review_tripwires_scan_profile: row shape, digest stability and sensitivity over a tmp git repo."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import review_tripwires_scan_profile as sp
from coordinator_core.ops.tests.test_review_tripwires import _repo

pytestmark = pytest.mark.complete

FILES = {
    "coordinator_core/ops/run_pass.py": "X = 1\n",
    "coordinator_core/ops/walk_me.py": "X = 2\n",
    "tests/test_names.py": 'X = ["run_pass.py"]\n',
    "tests/test_walk.py": 'from pathlib import Path\nPath("coordinator_core/ops").rglob("*.py")\n',
    "tests/test_plain.py": "X = 0\n",
}


def test_rows_cover_both_scanners_with_counts(tmp_path):
    repo = _repo(tmp_path, FILES)
    rows = {r.axis: r for r in sp.profile(repo, named=10, created=10, repeat=1)}
    assert set(rows) == {"literal_namers", "tree_scanners", "scan_tracked"}
    for r in rows.values():
        assert r.candidates == 3
        assert r.reads >= 3
        assert r.spawns == 0
        assert r.process_time_ms >= 0
        assert len(r.digest) == 64


def test_digest_tracks_output_and_is_repeatable(tmp_path):
    repo = _repo(tmp_path, FILES)
    first = {r.axis: r.digest for r in sp.profile(repo, repeat=1)}
    again = {r.axis: r.digest for r in sp.profile(repo, repeat=1)}
    assert first == again
    assert first["literal_namers"] == sp.digest_of({"tests/test_names.py": ["coordinator_core/ops/run_pass.py"]})
    assert first["tree_scanners"] == sp.digest_of(["tests/test_walk.py"])
    assert first["scan_tracked"] == sp.digest_of(["tests/test_names.py", "tests/test_walk.py"])
    assert sp.digest_of([]) !=sp.digest_of(["tests/test_walk.py"])


def test_over_bar_flag_and_exit_code(tmp_path, monkeypatch):
    repo = _repo(tmp_path, FILES)
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    monkeypatch.setattr(sp, "BAR_MS", -1.0)
    assert all(r.over_bar for r in sp.profile(repo, repeat=1))
    assert sp.main(["--repo", str(repo), "--repeat", "1"]) == 1
    monkeypatch.setattr(sp, "BAR_MS", 1e9)
    assert sp.main(["--repo", str(repo), "--repeat", "1"]) == 0
