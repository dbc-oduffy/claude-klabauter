"""Integrity defect #33: a halted run's dirt present at stage must never be staged as this run's own.

Case (a): HEAD clean, `write_baseline` already holds a peer's hunk (line 2), this run adds its own
hunk (line 8), and the run's kept blob is the whole contaminated file (the executor fingerprints the
file it edited). The peer hunk must be reported foreign so the commit stages HEAD plus line 8 only.
Was red on 916d5c7bd0: `hunk_provenance._unproduced` returns no foreign hunk because a kept blob equal to
the worktree short-circuits to "all produced". Fixed by the first-stage `foreign_at_baseline` manifest marker: only a path
marked dirty at the chain's FIRST baseline loses the kept-blob shortcut.
"""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import kept_work
from coordinator_core.ops.dispatch_emit.tests.test_hunk_provenance import (  # noqa: F401 -- fixture + helpers
    ME, PATH, _run, _text, world,
)


def _mark_foreign(world, data: bytes) -> None:
    d = world.root / "state" / "warp" / ME
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"foreign_at_baseline": {PATH: world.store(data)}}), encoding="utf-8"
    )


def test_peer_dirt_in_baseline_captured_by_own_kept_blob_is_still_foreign(world):
    world.head(_text({}))
    world.baseline(_text({2: "PEER"}))
    _mark_foreign(world, _text({2: "PEER"}))
    data = _text({2: "PEER", 8: "OWN"})
    world.tree(data)
    world.kept(kept_work.blob_sha(data))
    found = _run(world)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert found[0].detail.startswith("committed lines 2 ")


def test_own_hunk_after_foreign_baseline_is_still_credited(world):
    world.head(_text({}))
    world.baseline(_text({2: "PEER"}))
    _mark_foreign(world, _text({2: "PEER"}))
    data = _text({2: "PEER", 8: "OWN"})
    world.tree(data)
    world.kept(kept_work.blob_sha(data))
    (found,) = _run(world)
    assert "2" in found.detail and "8" not in found.detail.split("were")[0]


def test_unmarked_baseline_keeps_the_kept_blob_shortcut(world):
    """A restage's own earlier dirt carries no first-stage marker: the kept blob still credits it."""
    world.head(_text({}))
    world.baseline(_text({2: "OWN-EARLIER"}))
    data = _text({2: "OWN-EARLIER", 8: "OWN"})
    world.tree(data)
    world.kept(kept_work.blob_sha(data))
    assert _run(world) == []
