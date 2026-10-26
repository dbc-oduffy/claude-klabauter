"""run_write_set / unclaimed_dirty over a tmp_path run record shaped like run 5db511."""

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_write_set as rws
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops import warp_run_deliverable
from coordinator_core.session import claim_index, liveness

RUN = "warp-2026-10-26-sample-aaaaaa-20261026T000000-bbbbbb"
PLAN = """# p

## Tasks

```yaml plan-tasks
- id: C1
  title: t
  writes: [pkg/a.py, pkg/tests/test_a.py]
  disposition: open
- id: C2
  title: u
  writes: [pkg/b.py]
  disposition: open
```
"""


def _record(tmp_path, *, credit=True, declared=()):
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "docs/plans/p.md").write_text(PLAN, encoding="utf-8")
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / "state/sizings/s.yaml").write_text('plan: "docs/plans/p.md"\n', encoding="utf-8")
    base = record_dir(tmp_path, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps({"review_declared_paths": list(declared)}), encoding="utf-8")
    (base / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    (base / "leg-footprint.json").write_text(
        json.dumps({"writes": ["pkg/f.py", "scratch/x.txt", "pkg/a.py"]}), encoding="utf-8")
    if credit:
        (base / "credit.jsonl").write_text(json.dumps({"chunk": "C1", "evidence": "e"}) + "\n", encoding="utf-8")
    (base / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {"pkg/w.py": "red"}}}), encoding="utf-8")
    return base


class _Found(dict):
    recorded_kind = None


def _claims(monkeypatch, table, dead=()):
    """Stub the claim lookup; every claim is a write hold, sessions in `dead` are not live."""
    def lookup(paths, **kw):
        found = _Found({p: table.get(p, []) for p in paths})
        found.recorded_kind = {p: {s: "write" for s in table.get(p, [])} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid not in dead)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **kw: {})


def test_union_in_order_deduped_without_scratch(tmp_path, monkeypatch):
    _record(tmp_path, declared=["pkg/d.py"])
    _claims(monkeypatch, {})
    assert rws.run_write_set(tmp_path, RUN, "me") == (
        "pkg/d.py", "pkg/f.py", "pkg/a.py", "pkg/tests/test_a.py", "pkg/w.py")


def test_uncredited_chunk_writes_absent(tmp_path, monkeypatch):
    _record(tmp_path, credit=False)
    _claims(monkeypatch, {})
    got = rws.run_write_set(tmp_path, RUN, "me")
    assert "pkg/tests/test_a.py" not in got and "pkg/b.py" not in got


def test_live_peer_claim_excluded_own_and_unanswerable_kept(tmp_path, monkeypatch):
    _record(tmp_path)
    _claims(monkeypatch, {
        "pkg/f.py": ["other"], "pkg/a.py": ["me"], "pkg/w.py": [claim_index.UNANSWERABLE]})
    got = rws.run_write_set(tmp_path, RUN, "me")
    assert "pkg/f.py" not in got
    assert "pkg/a.py" in got and "pkg/w.py" in got


def test_dead_session_leftover_claim_keeps_path(tmp_path, monkeypatch):
    _record(tmp_path)
    _claims(monkeypatch, {"pkg/f.py": ["gone"], "pkg/a.py": ["live"]}, dead=("gone",))
    got = rws.run_write_set(tmp_path, RUN, "me")
    assert "pkg/f.py" in got and "pkg/a.py" not in got


def test_withhold_jsonl_path_included_without_halted_artifacts(tmp_path, monkeypatch):
    base = _record(tmp_path)
    (base / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    (base / "withhold.jsonl").write_text(
        json.dumps({"path": "pkg/j.py", "reason": "r", "at": "t"}) + "\n", encoding="utf-8")
    _claims(monkeypatch, {})
    got = rws.run_write_set(tmp_path, RUN, "me")
    assert "pkg/j.py" in got and "pkg/w.py" not in got


def test_missing_manifest_is_empty(tmp_path, monkeypatch):
    _claims(monkeypatch, {})
    assert rws.run_write_set(tmp_path, RUN, "me") == ()


def _git(tmp_path, *args):
    subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unclaimed_dirty_modified_untracked_and_foreign(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "clean.py").write_text("x", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x", encoding="utf-8")
    (tmp_path / "theirs.py").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "i")
    (tmp_path / "mod.py").write_text("y", encoding="utf-8")
    (tmp_path / "theirs.py").write_text("y", encoding="utf-8")
    (tmp_path / "new.py").write_text("n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("x", encoding="utf-8")
    _claims(monkeypatch, {"theirs.py": ["other"], "gone.py": ["dead"]}, dead=("dead",))
    got = rws.unclaimed_dirty(tmp_path, ["clean.py", "mod.py", "theirs.py", "new.py", "gone.py"], "me")
    assert sorted(got) == ["gone.py", "mod.py", "new.py"]
    assert rws.unclaimed_dirty(tmp_path, [], "me") == ()
