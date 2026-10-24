"""literal_namers takes candidates from the git index, never a directory walk."""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops import review_tripwires as rt
from coordinator_core.ops.tests.test_review_tripwires import EDITED, _repo

pytestmark = [pytest.mark.cadence]

BODY = 'X = "run_pass.py"\n'


def test_only_the_tracked_module_is_opened(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"tests/test_tracked.py": BODY})
    for i in range(5000):
        (repo / "tests" / f"test_untracked_{i}.py").write_text(BODY, encoding="utf-8")
    opened = []
    real_open = os.open

    def counting_open(path, *a, **kw):
        if os.path.basename(str(path)).startswith("test_"):
            opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(rt.os, "open", counting_open)
    assert list(rt.literal_namers(repo, [EDITED])) == ["tests/test_tracked.py"]
    assert len(opened) == 1


def test_untracked_module_holding_the_literal_is_not_selected(tmp_path):
    repo = _repo(tmp_path, {"tests/test_a.py": "X = 1\n"})
    (repo / "tests" / "test_b.py").write_text(BODY, encoding="utf-8")
    assert rt.literal_namers(repo, [EDITED]) == {}


def test_split_index_raises_scan_unavailable(tmp_path):
    repo = _repo(tmp_path, {"tests/test_a.py": BODY})
    (repo / ".git" / "sharedindex.abc").write_bytes(b"")
    with pytest.raises(rt.TripwireScanUnavailable):
        rt.literal_namers(repo, [EDITED])


def test_absent_index_is_empty_not_an_error(tmp_path):
    repo = _repo(tmp_path, {"tests/test_a.py": BODY}, track=False)
    (repo / ".git").mkdir()
    assert rt.literal_namers(repo, [EDITED]) == {}


def test_tracked_hidden_and_pycache_dirs_still_skipped(tmp_path):
    repo = _repo(
        tmp_path,
        {
            "tests/test_a.py": BODY,
            "tests/.h/test_b.py": BODY,
            "tests/__pycache__/test_c.py": BODY,
        },
    )
    assert list(rt.literal_namers(repo, [EDITED])) == ["tests/test_a.py"]
