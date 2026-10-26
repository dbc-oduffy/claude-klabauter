"""review.reverify classes every scratch/ write as foreign: a review agent that wrote under the
snapshot share dir and also under another run's scratch is reported, never reverted or removed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edit_source
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-scratch"
PREFIX = "prep-run-a"
BASE = "abc1234"
MOD = b"VALUE = 1\n"
SCRATCH_REL = "scratch/warp/other-run/synthesis.md"
SCRATCH_BODY = b"# live synthesis\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_bytes(MOD)
    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    (snap / _sha(MOD)).write_bytes(MOD)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": BASE,
            "recorded_at": "2026-10-19T00:00:00Z",
            "paths": ["pkg/mod.py"],
            "files": {"pkg/mod.py": _sha(MOD)},
        }),
        encoding="utf-8",
    )
    f = tmp_path / SCRATCH_REL
    f.parent.mkdir(parents=True)
    f.write_bytes(SCRATCH_BODY)
    by_agent = {"agentA": {f".coordinator-local/x/{PREFIX}/note.md", SCRATCH_REL}}
    latest = {SCRATCH_REL: "agentA"}
    monkeypatch.setattr(review_edit_source, "session_review_writes", lambda *_a, **_k: dict(latest))
    monkeypatch.setattr(review_edit_source, "session_agent_writes", lambda *_a, **_k: {k: set(v) for k, v in by_agent.items()})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    return tmp_path


def test_list_reports_scratch_write_as_foreign_not_created(world):
    listed = rr.review_reverify(world, base=BASE, slice_prefix=PREFIX, session_id=SESSION)
    assert SCRATCH_REL in listed["foreign"]
    assert SCRATCH_REL not in listed["created"]


def test_revert_never_removes_scratch_write(world):
    out = rr.review_reverify(
        world, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[f"{SCRATCH_REL}::test_x"],
    )
    assert out["error"] is None
    assert SCRATCH_REL not in [r["path"] for r in out["reverted"]]
    assert (world / SCRATCH_REL).read_bytes() == SCRATCH_BODY
    snap = Path(share_dir(str(world), SESSION)) / f"{PREFIX}.pre-review"
    assert not (snap / "removed" / "scratch").exists()
