"""session_live reads a holder dead when the harness registry shows the
holder's stable_pid process now serves a different session id."""

from __future__ import annotations

import json
import os
import subprocess
import time

import psutil
import pytest

from coordinator_core.session import claims, core, harness_registry as hr, liveness, scope, touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OLD_SID = "11111111-1111-4111-8111-111111111111"
NEW_SID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _reset_cache():
    liveness._registry_snapshot_cache = None
    yield
    liveness._registry_snapshot_cache = None


def _ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Git repo, sandboxed registry dir, and the test process as the live holder."""
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=tmp_path, **no_console_passthrough_kwargs())
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setattr(hr, "registry_dir", lambda: registry)
    pid = os.getpid()
    epoch = psutil.Process(pid).create_time()
    sdir = tmp_path / ".git" / "coordinator-sessions" / OLD_SID
    sdir.mkdir(parents=True)

    def write_meta(start_epoch=epoch, last_activity=None):
        (sdir / "meta.json").write_text(
            json.dumps(
                {
                    "stable_pid": str(pid),
                    "stable_pid_start_epoch": str(int(start_epoch)),
                    "last_activity": last_activity or core.now_iso(),
                }
            ),
            encoding="utf-8",
        )

    def write_record(sid, rec_epoch=epoch):
        (registry / f"{pid}.json").write_text(
            json.dumps({"sessionId": sid, "pid": pid, "procStart": _ticks(rec_epoch)}),
            encoding="utf-8",
        )

    write_meta()
    return {
        "repo": tmp_path, "registry": registry, "pid": pid, "epoch": epoch,
        "write_meta": write_meta, "write_record": write_record,
    }


def test_repointed_holder_reads_dead(world):
    world["write_record"](NEW_SID)
    assert hr.lookup(OLD_SID) is None
    assert liveness.session_live(OLD_SID, str(world["repo"])) is False


def test_same_sid_record_reads_live(world):
    world["write_record"](OLD_SID)
    assert liveness.session_live(OLD_SID, str(world["repo"])) is True
    assert liveness._holder_repointed(
        OLD_SID, str(world["pid"]), str(int(world["epoch"]))
    ) is False


def test_mismatched_epoch_falls_through_to_layer1(world):
    world["write_record"](NEW_SID, rec_epoch=world["epoch"] - 3600)
    assert liveness.session_live(OLD_SID, str(world["repo"])) is True


def test_unreadable_record_falls_through_to_layer1(world):
    (world["registry"] / f"{world['pid']}.json").write_text("{not json", encoding="utf-8")
    assert liveness.session_live(OLD_SID, str(world["repo"])) is True


def test_missing_record_falls_through_to_layer1(world):
    assert liveness.session_live(OLD_SID, str(world["repo"])) is True


_SUPERSEDED = (False, "harness-registry-superseded", None)
_STALE = "2000-01-01T00:00:00Z"


def _all_seams(repo):
    r = str(repo)
    return (
        liveness.live_session_ids(r),
        liveness.live_session_verdicts(r),
        liveness.session_verdict(OLD_SID, r),
        liveness.session_live_with_basis(OLD_SID, r),
    )


@pytest.mark.parametrize("last_activity", [None, _STALE], ids=["fresh", "stale"])
def test_superseded_sid_reads_dead_on_every_seam(world, last_activity):
    world["write_meta"](last_activity=last_activity)
    world["write_record"](NEW_SID)
    ids, verdicts, verdict, with_basis = _all_seams(world["repo"])
    assert OLD_SID not in ids
    assert verdicts[OLD_SID] == _SUPERSEDED
    assert verdict == _SUPERSEDED
    assert with_basis == (False, "harness-registry-superseded")


def test_registered_idle_stale_session_stays_live_on_every_seam(world):
    world["write_meta"](last_activity=_STALE)
    world["write_record"](OLD_SID)
    ids, verdicts, verdict, with_basis = _all_seams(world["repo"])
    assert OLD_SID in ids
    assert verdicts[OLD_SID][0] is True
    assert verdict[0] is True
    assert with_basis[0] is True


@pytest.mark.parametrize("case", ["mismatched-epoch", "unreadable", "missing"])
def test_corpus_seam_fails_open_like_session_live(world, case):
    if case == "mismatched-epoch":
        world["write_record"](NEW_SID, rec_epoch=world["epoch"] - 3600)
    elif case == "unreadable":
        (world["registry"] / f"{world['pid']}.json").write_text("{not json", encoding="utf-8")
    ids, verdicts, _, _ = _all_seams(world["repo"])
    assert OLD_SID in ids
    live, basis, _age = verdicts[OLD_SID]
    assert live is True
    assert basis in ("stable-pid", "stable-pid-shared")


def test_corpus_scan_reads_only_the_snapshot_built_index(world, monkeypatch):
    world["write_record"](NEW_SID)

    def _boom(*_a, **_k):
        raise AssertionError("corpus scan must not read the registry per pid")

    monkeypatch.setattr(hr, "record_for_pid", _boom)
    assert liveness.live_session_verdicts(str(world["repo"]))[OLD_SID] == _SUPERSEDED


def test_clear_claim_if_dead_clears_path_claim_of_repointed_holder(world):
    repo = world["repo"]
    target = "docs/some/artifact.md"
    sink = repo / ".git" / "coordinator-sessions" / OLD_SID / scope._TOUCH_RECORD_FILENAME
    touch_record.append_event(
        sink, session_id=OLD_SID, agent_id=None, verb=touch_record.VERB_TOUCH, path=target
    )
    base = str(repo / ".git" / "coordinator-sessions")

    # Live holder: refused.
    assert claims.clear_claim_if_dead("artifact", target, cwd=str(repo)) is False

    world["write_record"](NEW_SID)
    liveness._registry_snapshot_cache = None
    assert claims.clear_claim_if_dead("artifact", target, cwd=str(repo)) is True
    from coordinator_core.session import claim_index

    assert claim_index.lookup([target], sessions_dir=base)[target] == []
