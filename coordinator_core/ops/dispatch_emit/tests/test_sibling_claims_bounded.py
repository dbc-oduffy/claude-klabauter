"""The land path's sibling-claim work is bounded by fresh runs, and computed once per eviction."""

from __future__ import annotations

import builtins
import io
import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_evict as hte
from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_withheld_index, sibling_claims
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000b2"
OWN = "ask-own"
OLD_RUNS = 1500


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _halted(repo: Path, run_id: str, *withheld: str, sizing: str = "state/sizings/s.yaml") -> None:
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": list(withheld)}]}), encoding="utf-8")
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    for p in withheld:
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text("dirty\n", encoding="utf-8")
    marker = {"status": "halted", "run_artifacts": {"withheld": {p: "left in tree" for p in withheld}}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def _old_world(repo: Path) -> list:
    old = [f"ask-2024{1 + n % 12:02d}{1 + n % 28:02d}T{n % 24:02d}0000-{n:06x}" for n in range(OLD_RUNS)]
    for run_id in old:
        _halted(repo, run_id, "other/b.py", sizing=f"state/sizings/s{run_id}.yaml")
    return old


def _spy_opens(monkeypatch) -> list:
    seen: list = []
    real = io.open

    def spy(file, *a, **kw):
        seen.append(str(file).replace("\\", "/"))
        return real(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    return seen


def test_old_record_dirs_are_never_opened_and_a_fresh_halted_run_still_claims(repo, monkeypatch):
    old = _old_world(repo)
    fresh = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-ffffff"
    _halted(repo, fresh, "other/b.py")
    # A long-lived repo already holds both persistent ledgers; only the first-ever read builds them.
    halted_withheld_index.rebuild(repo)
    halted_hold_release.rebuild_sizing_runs(repo)
    seen = _spy_opens(monkeypatch)
    with halted_hold_release.release_scope():
        claims = sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py", "src/a.py"])
    assert claims == {"other/b.py": fresh}
    stale = [p for p in seen if any(f"/state/warp/{run_id}/" in p for run_id in old)]
    assert stale == []


def test_evict_computes_claims_once_for_all_paths(repo, monkeypatch):
    paths = [f"p{i}.py" for i in range(6)]
    for p in paths:
        (repo / p).write_bytes(b"bad\n")
    calls: list = []
    monkeypatch.setattr(
        sibling_claims, "sibling_run_claims", lambda root, run_id, cands, **kw: calls.append(list(cands)) or {}
    )
    monkeypatch.setattr(hte, "_reset_plan", lambda root, run_id, rel, data, head: (head, None))
    heads = {p: b"ok\n" for p in paths}
    hte.evict(repo, repo / "rec", paths, heads, run_id="me")
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted(paths)
