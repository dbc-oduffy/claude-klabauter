"""dc7a8a replay: a hand-land of a halted run's test alone, while its module (untracked at HEAD) stays
behind, lands and names the module under `stranded` (advisory, DR-474); landing both names nothing. A landed
path no run manifest declares is named under `ownerless`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ceremony.tests.test_hand_land import _call, _git, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit import stranded_sibling
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261114T000000-dc7a8a"
MODULE = "coordinator_core/ops/ceremony/hand_land.py"
TEST = "coordinator_core/ops/ceremony/tests/test_hand_land.py"
MODULE_BYTES = b"def hand_land(): ...\n"
TEST_BYTES = b"def test_it(): ...\n"


def _halted_run(repo, declared=(MODULE, TEST)):
    for rel, data in ((MODULE, MODULE_BYTES), (TEST, TEST_BYTES)):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    rec = repo / "state" / "warp" / RUN
    stranded_sibling.write(rec, {
        MODULE: {"base": None, "run": blob_sha(MODULE_BYTES)},
        TEST: {"base": None, "run": blob_sha(TEST_BYTES)},
    })
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": {p: None for p in declared}}), encoding="utf-8")


def test_landing_only_the_test_lands_and_names_the_untracked_module_stranded(repo):
    _halted_run(repo)
    head = _git(repo, "rev-parse", "HEAD")

    reply = _call({"repo_root": str(repo), "paths": [TEST], "message": "land test"})

    assert reply["ok"] is True, reply
    assert "refused" not in reply
    assert reply["stranded"] == [{"path": MODULE, "kind": "module", "reason": reply["stranded"][0]["reason"], "run_id": RUN}]
    assert _git(repo, "rev-parse", "HEAD") != head
    assert _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").splitlines() == [TEST]


def test_landing_both_paths_succeeds_with_empty_stranded_and_no_ownerless_note(repo):
    _halted_run(repo)

    reply = _call({"repo_root": str(repo), "paths": [TEST, MODULE], "message": "land both"})

    assert reply["ok"] is True, reply
    assert "stranded" not in reply
    assert "ownerless" not in reply
    assert sorted(_git(repo, "diff", "--name-only", "HEAD~1", "HEAD").splitlines()) == sorted([MODULE, TEST])


def test_a_sibling_already_at_its_run_blob_at_head_is_not_stranded(repo):
    _halted_run(repo)
    _git(repo, "add", "--", MODULE, TEST)
    _git(repo, "commit", "-q", "-m", "run product")
    (repo / MODULE).write_bytes(MODULE_BYTES + b"# follow-up edit\n")

    reply = _call({"repo_root": str(repo), "paths": [MODULE], "message": "module follow-up"})

    assert reply["ok"] is True, reply
    assert "stranded" not in reply


def test_a_run_that_changed_a_sibling_but_lost_it_names_it_lost(repo):
    _halted_run(repo)
    (repo / MODULE).write_bytes(b"def hand_land(): pass  # hunk lost\n")

    reply = _call({"repo_root": str(repo), "paths": [TEST], "message": "land test"})

    assert reply["ok"] is True, reply
    assert reply["stranded"][0]["reason"] == "lost"


def test_a_landing_unrelated_to_any_record_is_never_refused(repo):
    _halted_run(repo)
    (repo / "a.py").write_bytes(b"changed\n")

    reply = _call({"repo_root": str(repo), "paths": ["a.py"], "message": "unrelated"})

    assert reply["ok"] is True, reply


def test_a_landed_path_no_manifest_declares_is_named_ownerless_not_refused(repo):
    _halted_run(repo)
    (repo / "a.py").write_bytes(b"raw batch\n")

    reply = _call({"repo_root": str(repo), "paths": ["a.py"], "message": "raw batch"})

    assert reply["ok"] is True, reply
    assert reply["ownerless"] == ["a.py"]
