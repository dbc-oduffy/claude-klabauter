"""dispatch.close_halted_run_superseded: fail-closed superseded close of a halted run."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, halted_withheld_index, kept_work
from coordinator_core.ops.dispatch_emit import halted_run_superseded as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-x-20261026T000000-abcdef"


HALT_AT = "2026-01-10T00:00:00Z"
PRE_HALT = "2026-01-05T00:00:00Z"
POST_HALT = "2026-01-20T00:00:00Z"


def _git(repo: Path, *args: str, stdin: str | None = None, date: str | None = None) -> str:
    env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else None
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd=str(repo),
        check=True, capture_output=True, text=True, input=stdin, env=env, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict, date: str = POST_HALT) -> str:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", "c", date=date)
    return _git(repo, "rev-parse", "HEAD")


def _blob(repo: Path, text: str) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", stdin=text)


@pytest.fixture
def halted(tmp_path):
    """A repo with a halted RUN withholding a.txt and b.txt, base commit recorded, kept blobs stored."""
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"a.txt": "a0", "b.txt": "b0"}, date="2026-01-01T00:00:00Z")
    record = tmp_path / ask_contract.RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted=RUN, sizing_rel=None, run_base_sha=base)
    ask_contract.write_run_stage_at(record, "halted", now=HALT_AT)
    halted_run.record_withheld(record, {"a.txt": "kept", "b.txt": "kept"})
    kept_work.write(record, {"c": {"paths": {"a.txt": _blob(tmp_path, "a-kept"), "b.txt": _blob(tmp_path, "b-kept")}}}, None)
    return tmp_path, record, base


def _cpu() -> float:
    """Process time including reaped git children; `time.process_time` omits them."""
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _call(root, run_id=RUN):
    return op._handler({"run_id": run_id}, root)


def _indexed(root) -> bool:
    return (root / halted_withheld_index.INDEX_DIR / f"{RUN}.json").is_file()


def _unchanged(root, record, before):
    assert (record / halted_run.HALTED_FILE).read_text(encoding="utf-8") == before
    assert _indexed(root)


def test_all_paths_overwritten_closes_superseded_and_releases_index(halted):
    root, record, _ = halted
    assert _indexed(root)
    sha = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    start = _cpu()
    reply = _call(root)
    assert _cpu() - start < 0.5
    assert reply["ok"] is True
    assert reply["evidence"] == {"a.txt": sha, "b.txt": sha}
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker[halted_run.SUPERSEDED_EVIDENCE_FIELD] == {"a.txt": sha, "b.txt": sha}
    assert not _indexed(root)


def test_three_withheld_paths_spawn_count_and_process_time(halted, monkeypatch):
    """Three withheld paths still cost exactly three git spawns (anchor, log, cat-file), under 500ms."""
    root, record, _ = halted
    halted_run.record_withheld(record, {"a.txt": "kept", "b.txt": "kept", "c.txt": "kept"})
    kept_work.write(record, {"c": {"paths": {
        "a.txt": _blob(root, "a-kept"), "b.txt": _blob(root, "b-kept"), "c.txt": _blob(root, "c-kept"),
    }}}, None)
    sha = _commit(root, {"a.txt": "a-later", "b.txt": "b-later", "c.txt": "c-later"})
    spawned = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, args, *a, **kw):
            spawned.append(list(args)[:1])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    start = _cpu()
    reply = _call(root)
    elapsed = _cpu() - start
    monkeypatch.undo()
    assert reply["ok"] is True and set(reply["evidence"].values()) == {sha}
    assert len(spawned) == 3
    assert elapsed < 0.5


def test_path_with_no_post_halt_commit_refuses(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_head_blob_equal_to_withheld_blob_refuses(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-kept"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "b.txt" in reply["error"] and "equals" in reply["error"]
    _unchanged(root, record, before)


def test_resumed_marker_refuses(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    halted_run.mark_resumed(record)
    reply = _call(root)
    assert reply["ok"] is False and "resumed" in reply["error"]
    assert halted_run._read(record)["status"] == halted_run.STATUS_RESUMED


def test_resume_between_check_and_write_refuses_and_leaves_resumed(halted, monkeypatch):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    real = op._blob_ids

    def resume_then_ids(r, specs):
        halted_run.mark_resumed(record)
        return real(r, specs)

    monkeypatch.setattr(op, "_blob_ids", resume_then_ids)
    reply = _call(root)
    assert reply["ok"] is False and "changed under the write" in reply["error"]
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_RESUMED
    assert halted_run.SUPERSEDED_EVIDENCE_FIELD not in marker


def test_only_touch_predates_halt_refuses(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"}, date=PRE_HALT)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "no commit after the halt" in reply["error"]
    _unchanged(root, record, before)


def test_pre_halt_touch_on_one_path_is_not_evidence(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-pre"}, date=PRE_HALT)
    _commit(root, {"b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "a.txt" in reply["error"]
    _unchanged(root, record, before)


def test_no_run_stage_anchors_at_committed_halt_record(legacy):
    root, record, _ = legacy
    (record / ask_contract.RUN_STAGE_FILE).unlink()
    halt_commit = _commit(
        root, {f"{ask_contract.RECORD_DIR_ROOT}/{RUN}/{halted_run.HALTED_FILE}": (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")},
        date=HALT_AT,
    )
    _commit(root, {"a.txt": "pre-anchor-noise"}, date=PRE_HALT)
    assert op._halt_anchor(root, RUN) == (halt_commit, None)
    s1 = _commit(root, {"a.txt": "a-later"})
    s2 = _commit(root, {"b.txt": "b-later"})
    reply = _legacy_call(root, [s1, s2])
    assert reply["ok"] is True
    assert halted_run._read(record)[halted_run.SUPERSEDED_EVIDENCE_FIELD] == {"a.txt": s1, "b.txt": s2}


@pytest.mark.parametrize("stage", [None, "staged", "landed"])
def test_unresolvable_halt_anchor_refuses(halted, stage):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    stage_file = record / ask_contract.RUN_STAGE_FILE
    if stage is None:
        stage_file.unlink()
    else:
        ask_contract.write_run_stage_at(record, stage, now=HALT_AT)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "unanchored" in reply["error"]
    _unchanged(root, record, before)


def test_halt_before_any_commit_refuses(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    ask_contract.write_run_stage_at(record, "halted", now="2000-01-01T00:00:00Z")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "unanchored" in reply["error"]
    _unchanged(root, record, before)


def test_live_successor_refuses(halted, monkeypatch):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    from coordinator_core.ops.dispatch_emit import halted_run_landed

    monkeypatch.setattr(halted_run_landed, "_live_successor", lambda r, run: "warp-live-1")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "warp-live-1" in reply["error"]
    _unchanged(root, record, before)


def test_withheld_path_without_kept_blob_refuses(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    kept_work.write(record, {"c": {"paths": {"a.txt": _blob(root, "a-kept")}}}, None)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_unknown_run_and_unsafe_id_refuse(halted):
    root, _, _ = halted
    assert _call(root, "warp-nope-20261026T000000-abcdef")["ok"] is False
    assert _call(root, "../x")["ok"] is False


@pytest.fixture
def legacy(tmp_path):
    """A halted RUN withholding a.txt and b.txt with no kept blobs (a record written before withheld-prints)."""
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"a.txt": "a0", "b.txt": "b0"}, date="2026-01-01T00:00:00Z")
    record = tmp_path / ask_contract.RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted=RUN, sizing_rel=None, run_base_sha=base)
    ask_contract.write_run_stage_at(record, "halted", now=HALT_AT)
    halted_run.record_withheld(record, {"a.txt": "kept", "b.txt": "kept"})
    return tmp_path, record, base


def _legacy_call(root, shas, **extra):
    return op._handler({"run_id": RUN, "evidence_shas": shas, **extra}, root)


def test_legacy_record_closes_with_evidence_shas(legacy):
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    s2 = _commit(root, {"b.txt": "b-later"})
    start = _cpu()
    reply = _legacy_call(root, [s1, s2], memo_id="fed43b74f8")
    assert _cpu() - start < 0.5
    assert reply["ok"] is True and reply["memo_id"] == "fed43b74f8"
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker[halted_run.SUPERSEDED_EVIDENCE_FIELD] == {"a.txt": s1, "b.txt": s2}
    assert not _indexed(root)


def test_legacy_record_without_evidence_refuses(legacy):
    root, record, _ = legacy
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    for shas in (None, [], ["abc123"]):
        reply = op._handler({"run_id": RUN, **({} if shas is None else {"evidence_shas": shas})}, root)
        assert reply["ok"] is False
    _unchanged(root, record, before)


def test_legacy_withheld_path_untouched_since_halt_refuses(legacy):
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_evidence_not_covering_every_touched_path_refuses(legacy):
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    _commit(root, {"b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_evidence_sha_not_ancestor_of_head_refuses(legacy):
    root, record, _ = legacy
    _git(root, "checkout", "-q", "-b", "side")
    side = _commit(root, {"a.txt": "a-side", "b.txt": "b-side"})
    _git(root, "checkout", "-q", "-")
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [side])
    assert reply["ok"] is False and "ancestor" in reply["error"]
    _unchanged(root, record, before)


def test_record_with_kept_blobs_ignores_evidence_and_keeps_kept_blob_gate(halted):
    root, record, _ = halted
    s = _commit(root, {"a.txt": "a-later", "b.txt": "b-kept"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s])
    assert reply["ok"] is False and "equals" in reply["error"] and "legacy" not in reply
    _unchanged(root, record, before)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"chunks": "x"}', '{"chunks": {"c": {"paths": {}}}}'])
def test_present_but_unusable_kept_file_refuses_not_legacy(legacy, content):
    root, record, _ = legacy
    s = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    (record / kept_work.KEPT_FILE).write_text(content, encoding="utf-8")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s])
    assert reply["ok"] is False and "kept" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_evidence_commit_deleting_withheld_path_refuses(legacy):
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    _git(root, "rm", "-q", "b.txt")
    _git(root, "commit", "-q", "-m", "rm", date=POST_HALT)
    s2 = _git(root, "rev-parse", "HEAD")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1, s2])
    assert reply["ok"] is False and "deleted" in reply["error"] and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_prints_only_record_with_evidence_shas_refuses_not_legacy(legacy):
    root, record, _ = legacy
    s = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    (record / "withheld-prints.json").write_text("{}", encoding="utf-8")
    assert not (record / kept_work.KEPT_FILE).exists()
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s])
    assert reply["ok"] is False and "legacy" not in reply
    _unchanged(root, record, before)


def test_legacy_newest_touch_deletion_refuses_even_when_evidence_is_earlier(legacy):
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    _git(root, "rm", "-q", "b.txt")
    _git(root, "commit", "-q", "-m", "rm", date=POST_HALT)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "deleted" in reply["error"] and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_anchor_trap_pre_halt_touch_is_not_evidence(legacy):
    root, record, _ = legacy
    pre = _commit(root, {"a.txt": "a-pre", "b.txt": "b-pre"}, date=PRE_HALT)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [pre])
    assert reply["ok"] is False and "no commit after the halt" in reply["error"]
    _unchanged(root, record, before)
    post = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    reply = _legacy_call(root, [pre, post])
    assert reply["ok"] is False and pre[:12] in reply["error"]
    _unchanged(root, record, before)
