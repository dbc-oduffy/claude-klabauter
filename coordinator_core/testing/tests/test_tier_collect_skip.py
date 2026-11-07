"""tier_collect_skip: moved files are ignored only when the marker expression deselects `complete`."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.testing import tier_collect_skip as tcs

FAST = "not cadence and not complete and not pending_fix and not designed_red"
FULL = "not pending_fix and not designed_red"


@pytest.fixture
def root(tmp_path):
    ledger = tmp_path / tcs.LEDGER_RELPATH
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "moves": [
                    {"id": "pkg/tests/test_moved.py", "new_marker": "complete"},
                    {"id": "pkg/tests/test_cadence.py", "new_marker": "cadence"},
                ]
            }
        ),
        encoding="utf-8",
    )
    tcs._cache.clear()
    yield tmp_path
    tcs._cache.clear()


def _decide(root: Path, rel: str, markexpr):
    config = SimpleNamespace(option=SimpleNamespace(markexpr=markexpr), rootpath=root)
    return tcs._Plugin().pytest_ignore_collect(root / rel, config)


def test_fast_expr_ignores_moved_file(root):
    assert _decide(root, "pkg/tests/test_moved.py", FAST) is True


def test_full_expr_imports_moved_file(root):
    assert _decide(root, "pkg/tests/test_moved.py", FULL) is None
    assert _decide(root, "pkg/tests/test_moved.py", "") is None
    assert _decide(root, "pkg/tests/test_moved.py", None) is None


def test_unmoved_and_non_complete_moves_still_collect(root):
    assert _decide(root, "pkg/tests/test_other.py", FAST) is None
    assert _decide(root, "pkg/tests/test_cadence.py", FAST) is None


def test_path_outside_root_collects(root, tmp_path_factory):
    assert _decide(root, "../elsewhere/test_moved.py", FAST) is None


def test_missing_or_corrupt_ledger_skips_nothing(tmp_path):
    tcs._cache.clear()
    assert _decide(tmp_path, "pkg/tests/test_moved.py", FAST) is None
    (tmp_path / "state/test-tiers").mkdir(parents=True)
    (tmp_path / tcs.LEDGER_RELPATH).write_text("{not json", encoding="utf-8")
    tcs._cache.clear()
    assert _decide(tmp_path, "pkg/tests/test_moved.py", FAST) is None
    tcs._cache.clear()


def test_ledger_is_read_once(root):
    assert _decide(root, "pkg/tests/test_moved.py", FAST) is True
    (root / tcs.LEDGER_RELPATH).unlink()
    assert _decide(root, "pkg/tests/test_moved.py", FAST) is True


def test_register_installs_plugin_on_repo_root_conftest():
    registered = {}
    pm = SimpleNamespace(register=lambda plugin, name: registered.setdefault(name, plugin))
    tcs.register(SimpleNamespace(pluginmanager=pm))
    assert isinstance(registered["tier_collect_skip"], tcs._Plugin)
    assert "tier_collect_skip.register(config)" in (
        Path(__file__).resolve().parents[3] / "conftest.py"
    ).read_text(encoding="utf-8")


def test_live_ledger_moves_are_ignored_under_fast_expr():
    repo = Path(__file__).resolve().parents[3]
    tcs._cache.clear()
    ids = tcs.moved_complete_ids(repo)
    assert ids
    sample = sorted(ids)[0]
    assert _decide(repo, sample, FAST) is True
    assert _decide(repo, sample, FULL) is None
