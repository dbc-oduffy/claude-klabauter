"""review.reverify revert mode at default arguments (no sidecars) reverts only the run's own paths."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import review_edit_source
from coordinator_core.ops.review_reverify import review_reverify
from coordinator_core.ops.tests.test_review_reverify_head_clamp import BASE_FILES, PREFIX, SESSION, Repo

FOREIGN = "tests/test_foreign_wip.py"
FOREIGN_DIRT = b"def test_f():\r\n    assert False  # another run's WIP\r\n"
OWN = "tests/test_own_new.py"
OWN_BYTES = b"def test_o():\n    assert False\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch)


def _world(repo: Repo) -> str:
    repo.write(BASE_FILES)
    base = repo.commit("base")
    repo.snapshot(base, base, BASE_FILES)
    repo.write({FOREIGN: FOREIGN_DIRT, OWN: OWN_BYTES})
    repo.arm({FOREIGN: "a2", OWN: "a1"})
    by_agent = {
        "a1": {OWN, f".coordinator-local/x/{PREFIX}/note.md"},
        "a2": {FOREIGN},
    }
    repo.monkeypatch.setattr(review_edit_source, "session_agent_writes", lambda *_a, **_k: by_agent)
    return base


def test_default_call_leaves_foreign_dirt_untouched_and_removes_only_the_run_s_own_new_path(repo):
    base = _world(repo)
    out = review_reverify(
        repo.root, base=base, slice_prefix=PREFIX, session_id=SESSION,
        failing=[f"{FOREIGN}::test_f", f"{OWN}::test_o"],
    )
    assert out["error"] is None
    assert (repo.root / FOREIGN).read_bytes() == FOREIGN_DIRT
    node = f"{FOREIGN}::test_f"
    assert node in out["unattributed"] or node in out["foreign_red"] or node in out["foreign"]
    assert [(e["path"], e["action"]) for e in out["reverted"]] == [(OWN, "removed")]
    assert not (repo.root / OWN).exists()
    assert (repo.root / out["reverted"][0]["kept_at"]).read_bytes() == OWN_BYTES


def test_a_foreign_path_in_the_implicated_set_is_fenced_even_with_a_finding(repo, monkeypatch):
    base = _world(repo)
    from coordinator_core.ops import review_reverify as rr

    real = rr._implication
    monkeypatch.setattr(
        rr, "_implication",
        lambda root, ids, edited, created: {
            n: {*t, FOREIGN} for n, t in real(root, ids, edited, created).items()
        },
    )
    out = repo.revert(
        base, [f"{OWN}::test_o"],
        [{"id": "F1", "file": FOREIGN, "before": "assert False", "after": "assert True"}],
    )
    assert out["error"] is None
    assert (repo.root / FOREIGN).read_bytes() == FOREIGN_DIRT
    assert FOREIGN in [k["path"] for k in out["kept"]]
    assert all(e["path"] != FOREIGN for e in out["reverted"])
