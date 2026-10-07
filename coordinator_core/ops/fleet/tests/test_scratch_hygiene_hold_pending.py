"""MOVE-TO-HOLD.md parks a scratch entry: never purged, always nagged, and the summary advertises it."""

from __future__ import annotations

import os
import tempfile
import time

from coordinator_core.ops.fleet.scratch_hygiene import run_hygiene
from coordinator_core.ops.fleet.scratch_hygiene_plan import plan_purge

_OLD = time.time() - 30 * 86400


def _repo(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-sessions"))
    repo = tmp_path / "repo"
    for name, marker in (("parked", "Move-To-Hold.md"), ("junk", None)):
        d = repo / "scratch" / name
        d.mkdir(parents=True)
        (d / "data.bin").write_bytes(b"x" * 10)
        if marker:
            (d / marker).write_text("\nrunpod bands | example-retrieval-repo round 2 | after move\n", encoding="utf-8")
        for p in [*d.iterdir(), d]:
            os.utime(p, (_OLD, _OLD))
    return repo


def test_marker_entry_is_skipped_hold_pending_and_others_planned(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    actions = {r["path"]: r["action"] for r in plan_purge(repo, registry_dir=tmp_path / "no-sessions")}
    assert actions == {"scratch/parked": "skipped-hold-pending", "scratch/junk": "would-delete"}


def test_marker_entry_is_nagged_with_first_line_and_capability_advertised(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    out = run_hygiene({"repo_root": str(repo), "cadence": "workweek_start"})
    nag = [r for r in out["records"] if r.get("kind") == "hold-nag"]
    assert nag == [
        {
            "op": "fleet.scratch_hygiene",
            "kind": "hold-nag",
            "repo": "repo",
            "path": "scratch/parked",
            "bytes": nag[0]["bytes"],
            "age_days": nag[0]["age_days"],
            "readme": "runpod bands | example-retrieval-repo round 2 | after move",
            "finding": "hold-pending",
        }
    ]
    summary = out["records"][-1]
    assert summary["summary"] is True and "hold_pending_marker" in summary["capabilities"]
    assert out["contract_exit"] == 1


def test_apply_never_deletes_a_parked_entry(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    run_hygiene({"repo_root": str(repo), "cadence": "workday_start", "apply": True})
    assert (repo / "scratch" / "parked" / "data.bin").exists()
    assert not (repo / "scratch" / "junk").exists()
