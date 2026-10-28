"""sidecar_reap tombstones every sidecar it deletes, in both share homes, and spares live sessions."""

from __future__ import annotations

import io
import os
import time

from coordinator_core.review_trail.sweep_tombstone import swept_sidecars
from coordinator_core.sidecar_reap import reap_stale_sidecars

OLD = time.time() - 40 * 86400
HOMES = (".coordinator-local/subagent-share", "state/subagent-share")


def _seed(repo, sid):
    paths = []
    for home in HOMES:
        d = repo / home / sid
        d.mkdir(parents=True)
        f = d / "review.md"
        f.write_text("---\nstatus: complete\n---\n")
        os.utime(f, (OLD, OLD))
        paths.append(f)
    return paths


def test_dead_tombstoned_live_spared(tmp_path):
    repo = str(tmp_path)
    dead = _seed(tmp_path, "dead-sid")
    live = _seed(tmp_path, "live-sid")
    rc = reap_stale_sidecars(repo, lambda sid, cwd=None: sid == "live-sid", out=io.StringIO())
    assert rc == 0
    assert not any(p.exists() for p in dead)
    assert all(p.exists() for p in live)
    recs = swept_sidecars(repo, "dead-sid")
    assert {r["path"] for r in recs} == {f"{h}/dead-sid/review.md" for h in HOMES}
    assert all(r["sweeper"] == "sidecar_reap" for r in recs)
    assert swept_sidecars(repo, "live-sid") == ()


def test_dry_run_writes_no_tombstone(tmp_path):
    repo = str(tmp_path)
    dead = _seed(tmp_path, "dead-sid")
    reap_stale_sidecars(repo, lambda sid, cwd=None: False, dry_run=True, out=io.StringIO())
    assert all(p.exists() for p in dead)
    assert swept_sidecars(repo, "dead-sid") == ()
