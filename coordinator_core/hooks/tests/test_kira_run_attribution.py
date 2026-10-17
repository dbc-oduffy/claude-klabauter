"""The Kira close guard attributes an in-session engine run's code touches and
reviewer sidecar to the run, and still blocks the EM's own unrouted review."""

import json

import pytest

from coordinator_core.hooks import guard_kira_verdict_routed as g
from coordinator_core.session import touch_record

SID = "s1"
RUN = "ask-20261017T111023-cea26a"
RUN_FILE = "coordinator_core/ops/run_owned.py"
EM_FILE = "coordinator_core/ops/em_owned.py"


def _sidecar(share, name, body):
    (share / name).write_text("---\n" + body + "---\nbody\n", encoding="utf-8")


def _touch(sdir, path):
    line = touch_record.encode_line(
        session_id=SID, agent_id=None, verb=touch_record.VERB_TOUCH,
        path=path, kind=touch_record.KIND_WRITE,
    )
    with open(touch_record.sink_path(sdir), "ab") as fh:
        fh.write(line)


@pytest.fixture
def world(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    sdir = tmp_path / "sess"
    sdir.mkdir()
    warp = tmp_path / "state" / "warp" / RUN
    warp.mkdir(parents=True)
    (warp / "manifest.json").write_text(
        json.dumps({"review_declared_paths": [RUN_FILE], "rows": [{"writes": [RUN_FILE]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(g, "_share_dirs", lambda root, sid: [str(share)])
    monkeypatch.setattr(g, "_kira_repo_root", lambda payload: str(tmp_path))
    monkeypatch.setattr(g, "apply_guard_level", lambda name, d: d)
    monkeypatch.setattr("coordinator_core.session.core.session_dir", lambda sid, root: str(sdir))
    return share, sdir


def _run():
    return g._guard_kira_verdict_routed({"session_id": SID, "cwd": "."})


def _run_sidecars(share, reviewer_stem):
    _sidecar(
        share, f"{RUN}.review-wave-bookkeeping.md",
        f"agent_type: engine:review-wave-bookkeeping\nintegrated_from:\n- {reviewer_stem}\n",
    )
    _sidecar(share, f"{reviewer_stem}.md", "agent_type: coordinator:code-reviewer\n")


def _blocked(result) -> bool:
    return "no Kira" in json.dumps(result)


def test_run_touch_and_run_reviewer_do_not_trip(world):
    share, sdir = world
    _run_sidecars(share, "coordinator-code-reviewer.aaa")
    _touch(sdir, RUN_FILE)
    assert not _blocked(_run())


def test_em_own_touch_with_run_reviewer_still_trips_on_em_review(world):
    share, sdir = world
    _run_sidecars(share, "coordinator-code-reviewer.aaa")
    _sidecar(share, "coordinator-code-reviewer.bbb.md", "agent_type: coordinator:code-reviewer\n")
    _touch(sdir, EM_FILE)
    assert _blocked(_run())


def test_em_reviewer_with_only_run_touches_does_not_trip(world):
    share, sdir = world
    _run_sidecars(share, "coordinator-code-reviewer.aaa")
    _sidecar(share, "coordinator-code-reviewer.bbb.md", "agent_type: coordinator:code-reviewer\n")
    _touch(sdir, RUN_FILE)
    assert not _blocked(_run())


def test_filename_shape_alone_is_not_run_ownership(world):
    share, sdir = world
    _sidecar(share, "coordinator-code-reviewer.bbb.md", "agent_type: coordinator:code-reviewer\n")
    _touch(sdir, EM_FILE)
    assert _blocked(_run())


def test_unreadable_manifest_attributes_nothing(world, tmp_path):
    share, sdir = world
    (tmp_path / "state" / "warp" / RUN / "manifest.json").write_text("{", encoding="utf-8")
    _touch(sdir, RUN_FILE)
    in_scope = [(f"{RUN}.review-wave-bookkeeping.md", {"agent_type": "engine:review-wave-bookkeeping"})]
    _, paths = g._kira_run_attribution(str(tmp_path), in_scope)
    assert paths == frozenset()
    assert g._kira_session_authored_diff(str(tmp_path), SID, paths)
