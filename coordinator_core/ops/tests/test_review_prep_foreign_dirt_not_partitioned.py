"""review.prep at default worktree arguments partitions only the run's own paths.

Foreign dirt: a tracked file edited after stage whose only write claimant is a departed peer session
(no live hold). It must not reach undeclared_edits, the frozen diff or the partition.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.session import claim_index, liveness
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
DEAD_PEER = "sess-dead-peer"
RUN = "ask-20261113T000000-7c17f7"
OWN_EXTRA = "docs/own_extra.md"
FOREIGN = [f"product/foreign_{i}.md" for i in range(5)]


class _Found(dict):
    recorded_kind = None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _claims(monkeypatch, table):
    def lookup(paths, **kw):
        found = _Found({p: table.get(p, []) for p in paths})
        found.recorded_kind = {p: {s: "write" for s in table.get(p, [])} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **kw: {})


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    files = {"a/one.py": "x = 0\n", OWN_EXTRA: "own v1\n", "docs/plans/p.md": "---\nplan_id: pln-test-1\n---\n\n# p\n"}
    files.update({rel: "v1\n" for rel in FOREIGN})
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    record = record_dir(tmp_path, RUN)
    record.mkdir(parents=True, exist_ok=True)
    (record / "manifest.json").write_text(json.dumps({"review_declared_paths": ["a/one.py"]}), encoding="utf-8")
    snap.write_snapshot(tmp_path, record)
    return tmp_path


def _prep(repo: Path) -> dict:
    return rp._handler(
        {
            "run_id": RUN,
            "base": _git(repo, "rev-parse", "HEAD").strip(),
            "worktree": True,
            "plan_path": "docs/plans/p.md",
            "session_id": SESSION,
        },
        repo,
    )


def _dirty(repo: Path) -> None:
    (repo / "a" / "one.py").write_text("x = 1\n", encoding="utf-8")
    (repo / OWN_EXTRA).write_text("own v2\n", encoding="utf-8")
    for rel in FOREIGN:
        (repo / rel).write_text("peer edit\n", encoding="utf-8")


def test_foreign_claimed_dirt_stays_out_of_the_partition(repo, monkeypatch):
    _claims(monkeypatch, {rel: [DEAD_PEER] for rel in FOREIGN} | {OWN_EXTRA: [SESSION]})
    _dirty(repo)
    reply = _prep(repo)
    assert reply["error"] is None
    assert reply["undeclared_edits"] == [OWN_EXTRA]
    assert set(reply["reviewed_sha256"]) == {"a/one.py", OWN_EXTRA}
    diff = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert all(rel not in diff for rel in FOREIGN)


def test_unclaimed_dirt_is_foreign_when_the_index_tracks_the_run(repo, monkeypatch):
    _claims(monkeypatch, {"a/one.py": [SESSION], OWN_EXTRA: [SESSION]})
    _dirty(repo)
    reply = _prep(repo)
    assert reply["undeclared_edits"] == [OWN_EXTRA]
    assert set(reply["reviewed_sha256"]) == {"a/one.py", OWN_EXTRA}
    diff = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert all(rel not in diff for rel in FOREIGN)


def test_unwitnessed_dirt_is_foreign_when_the_index_has_no_witness_for_the_run(repo, monkeypatch):
    # Contract inverted: a file joins the partition only on positive evidence it is the run's;
    # an empty claim index is no evidence, so unclaimed dirt stays out.
    _claims(monkeypatch, {})
    _dirty(repo)
    reply = _prep(repo)
    assert reply["undeclared_edits"] == []
    assert set(reply["reviewed_sha256"]) == {"a/one.py"}


def test_own_session_claim_keeps_an_undeclared_edit_beside_a_peer_claim(repo, monkeypatch):
    _claims(monkeypatch, {OWN_EXTRA: [SESSION, DEAD_PEER], FOREIGN[0]: [DEAD_PEER]})
    _dirty(repo)
    assert OWN_EXTRA in _prep(repo)["undeclared_edits"]
    assert FOREIGN[0] not in _prep(repo)["undeclared_edits"]


def test_unanswerable_claim_lookup_is_no_witness(repo, monkeypatch):
    _claims(monkeypatch, {})
    monkeypatch.setattr(claim_index, "lookup", lambda paths, **kw: _Found({p: [claim_index.UNANSWERABLE] for p in paths}))
    _dirty(repo)
    assert _prep(repo)["undeclared_edits"] == []


def test_failing_attribution_fails_closed(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import land_run

    _claims(monkeypatch, {rel: [DEAD_PEER] for rel in FOREIGN} | {OWN_EXTRA: [SESSION]})

    def boom(*a, **kw):
        raise RuntimeError("attribution failed")

    monkeypatch.setattr(land_run, "_bounded_claim_lookup", boom)
    _dirty(repo)
    assert _prep(repo)["undeclared_edits"] == []
