import json
import os
import subprocess
import sys

import pytest

from coordinator_core import _settings_home
from coordinator_core.ops.dispatch_emit import live_run_index as idx
from coordinator_core.ops.dispatch_emit import sibling_footprint


@pytest.fixture(autouse=True)
def _isolated_machine_local(tmp_path, monkeypatch):
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml-home")


def _root(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


def test_claim_succeeds_when_no_overlap_and_records_footprint(tmp_path):
    root = _root(tmp_path)
    idx.add(root, "a", 1.0)
    assert idx.claim(root, "a", ["x/one.py", "x/two.py"]) == {}
    assert idx.read(root, 1000) == {"a": frozenset({"x/one.py", "x/two.py"})}


def test_claim_refused_on_overlap_writes_nothing(tmp_path):
    root = _root(tmp_path)
    idx.add(root, "a", 1.0)
    idx.add(root, "b", 2.0)
    assert idx.claim(root, "a", ["x/one.py"]) == {}
    assert idx.claim(root, "b", ["x"]) == {"a": ["x/one.py"]}
    assert idx.read(root, 1000) == {"a": frozenset({"x/one.py"}), "b": None}


def test_claim_ignores_minted_unstaged_sibling_and_own_prior_claim(tmp_path):
    root = _root(tmp_path)
    idx.add(root, "a", 1.0)
    idx.add(root, "b", 2.0)
    assert idx.claim(root, "b", ["x/one.py"]) == {}
    assert idx.claim(root, "b", ["x/one.py", "y.py"]) == {}


def test_claim_without_common_dir_is_unclaimed_not_empty(tmp_path):
    assert idx.claim(tmp_path / "nowhere", "a", ["x.py"]) is None


def test_claim_unclaimed_when_lock_not_taken(tmp_path, monkeypatch):
    from coordinator_core import locked_write

    root = _root(tmp_path)

    def boom(*a, **k):
        raise locked_write.LockTimeout("held")

    monkeypatch.setattr(locked_write, "held_lock", boom)
    assert idx.claim(root, "a", ["x.py"]) is None
    assert idx.read(root, 1000) in ({}, idx.MISSING)


def test_claim_after_remove_frees_the_path(tmp_path):
    root = _root(tmp_path)
    idx.add(root, "a", 1.0)
    idx.add(root, "b", 2.0)
    assert idx.claim(root, "a", ["x.py"]) == {}
    idx.remove(root, "a")
    assert idx.claim(root, "b", ["x.py"]) == {}


def test_claim_drops_ended_hit_but_still_refuses_live_one(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    root = _root(tmp_path)
    for rid in ("a", "b", "c"):
        idx.add(root, rid, 1.0)
    assert idx.claim(root, "a", ["x/one.py"]) == {}
    assert idx.claim(root, "b", ["y/two.py"]) == {}
    monkeypatch.setattr(warp_live, "run_is_over", lambda r, rid: rid == "a")
    assert idx.claim(root, "c", ["x"]) == {}
    assert idx.read(root, 1000)["c"] == frozenset({"x"})
    idx.remove(root, "c")
    idx.add(root, "c", 3.0)
    assert idx.claim(root, "c", ["y"]) == {"b": ["y/two.py"]}
    assert idx.read(root, 1000)["c"] is None


def test_claim_custom_ended_predicate_overrides_default(tmp_path):
    root = _root(tmp_path)
    idx.add(root, "a", 1.0)
    idx.add(root, "b", 2.0)
    assert idx.claim(root, "a", ["x.py"]) == {}
    assert idx.claim(root, "b", ["x.py"], ended=lambda rid: False) == {"a": ["x.py"]}
    assert idx.claim(root, "b", ["x.py"], ended=lambda rid: rid == "a") == {}


def test_refused_claim_leaves_no_marker_and_remove_releases_markers(tmp_path):
    root = _root(tmp_path)
    claims = idx._claims_dir(idx.index_path(root))
    idx.add(root, "a", 1.0)
    idx.add(root, "b", 2.0)
    assert idx.claim(root, "a", ["x/one.py"]) == {}
    assert idx.claim(root, "b", ["y.py", "x"]) == {"a": ["x/one.py"]}
    assert len(list(claims.iterdir())) == 1
    idx.remove(root, "a")
    assert list(claims.iterdir()) == []


def _unreadable_marker(root, rel, age_seconds):
    claims = idx._claims_dir(idx.index_path(root))
    claims.mkdir(parents=True, exist_ok=True)
    marker = claims / idx.marker_name(rel)
    marker.write_bytes(b"")
    stamp = marker.stat().st_mtime - age_seconds
    os.utime(marker, (stamp, stamp))
    return marker


def test_aged_unreadable_marker_for_wanted_path_is_reclaimed(tmp_path):
    root = _root(tmp_path)
    marker = _unreadable_marker(root, "x/one.py", idx.ABANDONED_MARKER_SECONDS + 60)
    assert idx.claim(root, "a", ["x/one.py"]) == {}
    assert json.loads(marker.read_bytes())["run"] == "a"


def test_fresh_unreadable_marker_for_wanted_path_still_refuses(tmp_path):
    root = _root(tmp_path)
    marker = _unreadable_marker(root, "x/one.py", 0)
    assert idx.claim(root, "a", ["x/one.py"]) == {"?": ["x/one.py"]}
    assert marker.exists()


_CHILD = """
import sys
from pathlib import Path
from coordinator_core import _settings_home
_settings_home.machine_local_dir = lambda: Path(sys.argv[2]) / "ml-home"
from coordinator_core.ops.dispatch_emit import live_run_index as idx, live_run_writes
root = Path(sys.argv[1])
rid = sys.argv[3]
live_run_writes.index_mint(root, rid, 1.0)
r = idx.claim(root, rid, ["shared/file.py"])
print("CLAIMED" if r == {} else "REFUSED" if r else "UNCLAIMED")
"""


def test_two_processes_claiming_same_path_exactly_one_wins(tmp_path):
    root = _root(tmp_path)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(root), str(tmp_path), rid],
            stdout=subprocess.PIPE, text=True,
        )
        for rid in ("p1", "p2")
    ]
    outs = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
    assert outs == ["CLAIMED", "REFUSED"]


_PLAN = """---
title: t
---

## Tasks

```yaml plan-tasks
- id: C1
  title: t
  writes:
    - a/b.py
    - a/c.py
```
"""


def test_inferred_writes_from_sizing_plan_spine(tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "p.md").write_text(_PLAN, encoding="utf-8")
    got = sibling_footprint.inferred_writes(tmp_path, {"plan": "docs/plans/p.md"})
    assert got == ("a/b.py", "a/c.py")


def test_inferred_writes_empty_without_plan_edge_or_outside_plans(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "p.md").write_text(_PLAN, encoding="utf-8")
    assert sibling_footprint.inferred_writes(tmp_path, {}) == ()
    assert sibling_footprint.inferred_writes(tmp_path, {"plan": "docs/p.md"}) == ()
    assert sibling_footprint.inferred_writes(tmp_path, {"plan": "docs/plans/missing.md"}) == ()
