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
def halted(tmp_path: Path) -> tuple[Path, Path, str]:
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


def _no_close_commit(monkeypatch):
    """Count and time the classification path alone: the closure commit has its own spawns."""
    from coordinator_core.ops.dispatch_emit import halted_run_close_commit

    monkeypatch.setattr(halted_run_close_commit, "commit_close_records", lambda *a, **k: {})


def _head(root) -> str:
    return _git(root, "rev-parse", "HEAD")


def _assert_closure_commit(root, record, base):
    """HEAD is a commit touching the run's halted.json and naming *base* as ``Checkpoint-Run-Closed``."""
    rel = (record / halted_run.HALTED_FILE).relative_to(root).as_posix()
    assert rel in _git(root, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert f"Checkpoint-Run-Closed: {base}" in _git(root, "log", "-1", "--format=%B", "HEAD")


def _indexed(root) -> bool:
    return (root / halted_withheld_index.INDEX_DIR / f"{RUN}.json").is_file()


def _unchanged(root, record, before) -> None:
    assert (record / halted_run.HALTED_FILE).read_text(encoding="utf-8") == before
    assert _indexed(root)


def test_ok_close_lands_closure_commit_with_run_closed_trailer(halted):
    root, record, base = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    reply = _call(root)
    assert reply["ok"] is True and reply["records_commit"] == _head(root)
    _assert_closure_commit(root, record, base)
    assert _git(root, "status", "--porcelain", "--", record.relative_to(root).as_posix()) == ""


def test_refused_close_leaves_head_unchanged(halted):
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later"})
    head = _head(root)
    reply = _call(root)
    assert reply["ok"] is False and "records_commit" not in reply
    assert _head(root) == head


def test_all_paths_overwritten_closes_superseded_and_releases_index(halted, monkeypatch):
    root, record, _ = halted
    assert _indexed(root)
    sha = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    _no_close_commit(monkeypatch)
    start = _cpu()
    reply = _call(root)
    assert _cpu() - start < 0.5
    assert reply["ok"] is True
    assert reply["evidence"] == {"a.txt": sha, "b.txt": sha}
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker[halted_run.SUPERSEDED_EVIDENCE_FIELD] == {"a.txt": sha, "b.txt": sha}
    assert not _indexed(root)


def test_three_withheld_paths_spawn_count_and_process_time(halted, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three withheld paths still cost exactly three git spawns (anchor, log, cat-file), under 500ms."""
    root, record, _ = halted
    halted_run.record_withheld(record, {"a.txt": "kept", "b.txt": "kept", "c.txt": "kept"})
    kept_work.write(record, {"c": {"paths": {
        "a.txt": _blob(root, "a-kept"), "b.txt": _blob(root, "b-kept"), "c.txt": _blob(root, "c-kept"),
    }}}, None)
    sha = _commit(root, {"a.txt": "a-later", "b.txt": "b-later", "c.txt": "c-later"})
    _no_close_commit(monkeypatch)
    spawned = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, args, *a, **kw) -> None:
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


def test_path_with_no_post_halt_commit_refuses(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_head_blob_equal_to_withheld_blob_refuses(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-kept", "b.txt": "b-kept"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "equals" in reply["error"] and "record_halted_run_landed" in reply["error"]
    _unchanged(root, record, before)


def test_resumed_marker_without_live_successor_closes_superseded(halted) -> None:
    root, record, base = halted
    sha = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    halted_run.mark_resumed(record)
    reply = _call(root)
    assert reply["ok"] is True and reply["evidence"] == {"a.txt": sha, "b.txt": sha}
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker[halted_run.SUPERSEDED_EVIDENCE_FIELD] == {"a.txt": sha, "b.txt": sha}
    assert reply["records_commit"] == _head(root)
    _assert_closure_commit(root, record, base)
    assert not _indexed(root)


def test_resumed_marker_with_live_successor_refuses_naming_it(halted, monkeypatch: pytest.MonkeyPatch) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    halted_run.mark_resumed(record)
    from coordinator_core.ops.dispatch_emit import halted_run_landed

    monkeypatch.setattr(halted_run_landed, "_live_successor", lambda r, run: "warp-live-1")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    head = _head(root)
    reply = _call(root)
    assert reply["ok"] is False and "warp-live-1" in reply["error"]
    assert (record / halted_run.HALTED_FILE).read_text(encoding="utf-8") == before
    assert _head(root) == head


def test_resumed_marker_with_unsuperseded_path_still_refuses(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later"})
    halted_run.mark_resumed(record)
    reply = _call(root)
    assert reply["ok"] is False and "b.txt" in reply["error"]
    assert halted_run._read(record)["status"] == halted_run.STATUS_RESUMED


@pytest.mark.parametrize("status", [halted_run.STATUS_LANDED, halted_run.STATUS_SUPERSEDED, halted_run.STATUS_CLOSED])
def test_terminal_marker_status_refuses(halted, status) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    halted_run._write(record, {**halted_run._read(record), "status": status})
    reply = _call(root)
    assert reply["ok"] is False and status in reply["error"]
    assert halted_run._read(record)["status"] == status


def test_resume_between_check_and_write_refuses_and_leaves_resumed(halted, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_only_touch_predates_halt_refuses(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"}, date=PRE_HALT)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "no commit after the halt" in reply["error"]
    _unchanged(root, record, before)


def test_pre_halt_touch_on_one_path_is_not_evidence(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-pre"}, date=PRE_HALT)
    _commit(root, {"b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "a.txt" in reply["error"]
    _unchanged(root, record, before)


def test_no_run_stage_anchors_at_committed_halt_record(legacy) -> None:
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
def test_unresolvable_halt_anchor_refuses(halted, stage) -> None:
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


def test_halt_before_any_commit_refuses(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    ask_contract.write_run_stage_at(record, "halted", now="2000-01-01T00:00:00Z")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "unanchored" in reply["error"]
    _unchanged(root, record, before)


def test_live_successor_refuses(halted, monkeypatch: pytest.MonkeyPatch) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    from coordinator_core.ops.dispatch_emit import halted_run_landed

    monkeypatch.setattr(halted_run_landed, "_live_successor", lambda r, run: "warp-live-1")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "warp-live-1" in reply["error"]
    _unchanged(root, record, before)


def test_withheld_path_without_kept_entry_closes_as_unkept_with_reason(halted) -> None:
    root, record, base = halted
    sha = _commit(root, {"a.txt": "a-later"})
    kept_work.write(record, {"c": {"paths": {"a.txt": _blob(root, "a-kept")}}}, None)
    reply = _call(root)
    assert reply["ok"] is True
    assert reply["dispositions"]["b.txt"] == {"disposition": op.DISPOSITION_UNKEPT, "reason": op._UNKEPT_REASON}
    assert reply["dispositions"]["a.txt"] == {"disposition": halted_run.DISPOSITION_SUPERSEDED, "sha": sha}
    assert reply["evidence"] == {"a.txt": sha}
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker[halted_run.PATH_DISPOSITIONS_FIELD]["b.txt"]["disposition"] == op.DISPOSITION_UNKEPT
    assert reply["records_commit"] == _head(root)
    _assert_closure_commit(root, record, base)
    assert not _indexed(root)


def test_unkept_path_closes_even_when_untouched_and_no_other_path_superseded(halted) -> None:
    root, record, _ = halted
    kept_work.write(record, {"c": {"paths": {"a.txt": _blob(root, "a-kept")}}}, None)
    _commit(root, {"a.txt": "a-kept"})
    reply = _call(root)
    assert reply["ok"] is True and reply["evidence"] == {}
    assert reply["dispositions"]["a.txt"]["disposition"] == halted_run.DISPOSITION_LANDED
    assert reply["dispositions"]["b.txt"]["disposition"] == op.DISPOSITION_UNKEPT


def test_kept_file_with_no_entry_for_any_withheld_path_still_refuses(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    kept_work.write(record, {"c": {"paths": {"other.txt": _blob(root, "o")}}}, None)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "yields no kept blob" in reply["error"]
    _unchanged(root, record, before)


def test_unkept_sibling_does_not_excuse_an_untouched_kept_path(halted) -> None:
    root, record, _ = halted
    kept_work.write(record, {"c": {"paths": {"a.txt": _blob(root, "a-kept")}}}, None)
    reply = _call(root)
    assert reply["ok"] is False and "a.txt" in reply["error"]
    assert _indexed(root)


def test_unknown_run_and_unsafe_id_refuse(halted) -> None:
    root, _, _ = halted
    assert _call(root, "warp-nope-20261026T000000-abcdef")["ok"] is False
    assert _call(root, "../x")["ok"] is False


@pytest.fixture
def legacy(tmp_path: Path) -> tuple[Path, Path, str]:
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


def _commit_halt_record(root, record) -> None:
    rel = (record / halted_run.HALTED_FILE).relative_to(root).as_posix()
    _git(root, "add", "--", rel)
    _git(root, "commit", "-q", "-m", "halt", date=PRE_HALT)


def test_legacy_anchors_at_first_record_commit_when_stage_stamp_postdates_ship(legacy) -> None:
    root, record, _ = legacy
    _commit_halt_record(root, record)
    s1 = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"}, date="2026-01-08T00:00:00Z")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is True and reply["evidence"] == {"a.txt": s1, "b.txt": s1}


def test_legacy_first_record_anchor_still_refuses_untouched_path(legacy) -> None:
    root, record, _ = legacy
    _commit_halt_record(root, record)
    s1 = _commit(root, {"a.txt": "a-later"}, date="2026-01-08T00:00:00Z")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "b.txt" in reply["error"]
    assert halted_run._read(record)["status"] == halted_run.STATUS_HALTED


def test_legacy_ok_close_lands_closure_commit(legacy):
    root, record, base = legacy
    s1 = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is True and reply["records_commit"] == _head(root)
    _assert_closure_commit(root, record, base)


def test_legacy_refused_close_leaves_head_unchanged(legacy):
    root, record, _ = legacy
    head = _head(root)
    assert _legacy_call(root, [])["ok"] is False
    assert _head(root) == head


def test_legacy_record_closes_with_evidence_shas(legacy, monkeypatch):
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    s2 = _commit(root, {"b.txt": "b-later"})
    _no_close_commit(monkeypatch)
    start = _cpu()
    reply = _legacy_call(root, [s1, s2], memo_id="fed43b74f8")
    assert _cpu() - start < 0.5
    assert reply["ok"] is True and reply["memo_id"] == "fed43b74f8"
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker[halted_run.SUPERSEDED_EVIDENCE_FIELD] == {"a.txt": s1, "b.txt": s2}
    assert not _indexed(root)


def test_legacy_record_without_evidence_refuses(legacy) -> None:
    root, record, _ = legacy
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    for shas in (None, [], ["abc123"]):
        reply = op._handler({"run_id": RUN, **({} if shas is None else {"evidence_shas": shas})}, root)
        assert reply["ok"] is False
    _unchanged(root, record, before)


def test_legacy_withheld_path_untouched_since_halt_refuses(legacy) -> None:
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_evidence_not_covering_every_touched_path_refuses(legacy) -> None:
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    _commit(root, {"b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_evidence_sha_not_ancestor_of_head_refuses(legacy) -> None:
    root, record, _ = legacy
    _git(root, "checkout", "-q", "-b", "side")
    side = _commit(root, {"a.txt": "a-side", "b.txt": "b-side"})
    _git(root, "checkout", "-q", "-")
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [side])
    assert reply["ok"] is False and "ancestor" in reply["error"]
    _unchanged(root, record, before)


def test_record_with_kept_blobs_ignores_evidence_and_keeps_kept_blob_gate(halted) -> None:
    root, record, _ = halted
    s = _commit(root, {"a.txt": "a-kept", "b.txt": "b-kept"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s])
    assert reply["ok"] is False and "equals" in reply["error"] and "legacy" not in reply
    _unchanged(root, record, before)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"chunks": "x"}', '{"chunks": {"c": {"paths": {}}}}'])
def test_present_but_unusable_kept_file_refuses_not_legacy(legacy, content) -> None:
    root, record, _ = legacy
    s = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    (record / kept_work.KEPT_FILE).write_text(content, encoding="utf-8")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s])
    assert reply["ok"] is False and "kept" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_evidence_commit_deleting_withheld_path_refuses(legacy) -> None:
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later"})
    _git(root, "rm", "-q", "b.txt")
    _git(root, "commit", "-q", "-m", "rm", date=POST_HALT)
    s2 = _git(root, "rev-parse", "HEAD")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1, s2])
    assert reply["ok"] is False and "deleted" in reply["error"] and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_prints_only_record_with_evidence_shas_refuses_not_legacy(legacy) -> None:
    root, record, _ = legacy
    s = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    (record / "withheld-prints.json").write_text("{}", encoding="utf-8")
    assert not (record / kept_work.KEPT_FILE).exists()
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s])
    assert reply["ok"] is False and "legacy" not in reply
    _unchanged(root, record, before)


def test_legacy_touched_then_reverted_path_refuses(legacy) -> None:
    root, record, _ = legacy
    _commit(root, {"a.txt": "a-later"})
    s1 = _commit(root, {"a.txt": "a0", "b.txt": "b-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "a.txt" in reply["error"] and "reverted" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_newest_touch_deletion_refuses_even_when_evidence_is_earlier(legacy) -> None:
    root, record, _ = legacy
    s1 = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    _git(root, "rm", "-q", "b.txt")
    _git(root, "commit", "-q", "-m", "rm", date=POST_HALT)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "deleted" in reply["error"] and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_legacy_anchor_trap_pre_halt_touch_is_not_evidence(legacy) -> None:
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


def _scratch_halted(root, record, extra) -> None:
    """Withhold `extra` (path -> text) on top of a.txt/b.txt; kept blobs stored for the kept route."""
    withheld = {"a.txt": "kept", "b.txt": "kept", **{p: "kept" for p in extra}}
    halted_run.record_withheld(record, withheld)
    paths = {"a.txt": _blob(root, "a-kept"), "b.txt": _blob(root, "b-kept"), **{p: _blob(root, t) for p, t in extra.items()}}
    kept_work.write(record, {"c": {"paths": paths}}, None)


def test_per_path_mixed_landed_and_superseded_closes_and_records_every_path(halted) -> None:
    root, record, _ = halted
    sha = _commit(root, {"a.txt": "a-later", "b.txt": "b-kept"})
    reply = _call(root)
    assert reply["ok"] is True
    assert reply["evidence"] == {"a.txt": sha}
    disp = reply["dispositions"]
    assert disp["a.txt"] == {"disposition": halted_run.DISPOSITION_SUPERSEDED, "sha": sha}
    assert disp["b.txt"]["disposition"] == halted_run.DISPOSITION_LANDED
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker[halted_run.PATH_DISPOSITIONS_FIELD] == disp
    assert not _indexed(root)


MISSING_BLOB = "5f1d0587" + "0" * 32


def _lose_kept_blob(root, record, path) -> None:
    """Re-record `path`'s kept blob as an id that was never written to the object store."""
    paths = {"a.txt": _blob(root, "a-kept"), "b.txt": _blob(root, "b-kept"), path: MISSING_BLOB}
    kept_work.write(record, {"c": {"paths": paths}}, None)


def test_missing_kept_blob_is_lost_with_reason_and_needs_no_superseding_commit(halted) -> None:
    root, record, _ = halted
    _lose_kept_blob(root, record, "a.txt")
    sha = _commit(root, {"b.txt": "b-later"})
    reply = _call(root)
    assert reply["ok"] is True
    assert reply["dispositions"]["a.txt"] == {"disposition": halted_run.DISPOSITION_LOST, "reason": op._LOST_REASON}
    assert reply["dispositions"]["b.txt"] == {"disposition": halted_run.DISPOSITION_SUPERSEDED, "sha": sha}
    assert reply["evidence"] == {"b.txt": sha}
    assert not _indexed(root)


def test_lost_path_untouched_since_halt_closes_with_landed_rest(halted) -> None:
    root, record, _ = halted
    _lose_kept_blob(root, record, "a.txt")
    _commit(root, {"b.txt": "b-kept"})
    reply = _call(root)
    assert reply["ok"] is True and reply["evidence"] == {}
    assert reply["dispositions"]["a.txt"]["disposition"] == halted_run.DISPOSITION_LOST
    assert reply["dispositions"]["b.txt"]["disposition"] == halted_run.DISPOSITION_LANDED
    assert halted_run._read(record)["status"] == halted_run.STATUS_SUPERSEDED


def test_present_kept_blob_unrelated_to_lost_path_still_refuses_untouched(halted) -> None:
    root, record, _ = halted
    reply = _call(root)
    assert reply["ok"] is False and "a.txt" in reply["error"]
    assert _indexed(root)


def test_per_path_landed_untouched_after_anchor_still_landed(halted) -> None:
    root, record, _ = halted
    pre = _commit(root, {"b.txt": "b-kept"}, date=PRE_HALT)
    assert pre
    sha = _commit(root, {"a.txt": "a-later"})
    reply = _call(root)
    assert reply["ok"] is True and reply["evidence"] == {"a.txt": sha}
    assert reply["dispositions"]["b.txt"]["disposition"] == halted_run.DISPOSITION_LANDED


def test_per_path_kept_route_scratch_never_tracked_closes(halted) -> None:
    root, record, _ = halted
    _scratch_halted(root, record, {"scratch/x/diag.log": "log"})
    sha = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    reply = _call(root)
    assert reply["ok"] is True
    assert reply["dispositions"]["scratch/x/diag.log"]["disposition"] == halted_run.DISPOSITION_SCRATCH
    assert "scratch/x/diag.log" not in reply["evidence"] and reply["evidence"]["a.txt"] == sha


def test_per_path_kept_route_spawn_count_unchanged(halted, monkeypatch: pytest.MonkeyPatch) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later", "b.txt": "b-kept"})
    _no_close_commit(monkeypatch)
    spawned = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, args, *a, **kw) -> None:
            spawned.append(list(args)[:1])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    start = _cpu()
    reply = _call(root)
    elapsed = _cpu() - start
    monkeypatch.undo()
    assert reply["ok"] is True and len(spawned) == 3 and elapsed < 0.5


def test_per_path_deleted_at_head_in_mixed_run_refuses_naming_it(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later"})
    _git(root, "rm", "-q", "b.txt")
    _git(root, "commit", "-q", "-m", "rm", date=POST_HALT)
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "b.txt" in reply["error"] and "a.txt" not in reply["error"]
    _unchanged(root, record, before)


def test_per_path_two_unclassifiable_paths_are_both_named(halted) -> None:
    root, record, _ = halted
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root)
    assert reply["ok"] is False and "a.txt" in reply["error"] and "b.txt" in reply["error"]
    _unchanged(root, record, before)


def test_per_path_tracked_scratch_untouched_after_anchor_refuses(halted) -> None:
    root, record, _ = halted
    _commit(root, {"a.txt": "a-later"})
    (root / "scratch").mkdir()
    _commit(root, {"scratch/t.log": "t"}, date=PRE_HALT)
    _scratch_halted(root, record, {"scratch/t.log": "t2"})
    _commit(root, {"b.txt": "b-later"})
    reply = _call(root)
    assert reply["ok"] is False and "scratch/t.log" in reply["error"]
    assert halted_run._read(record)["status"] == halted_run.STATUS_HALTED


def test_per_path_scratch_only_with_landed_rest_refuses_pointing_to_landed_op(halted) -> None:
    root, record, _ = halted
    _scratch_halted(root, record, {"scratch/x.log": "log"})
    _commit(root, {"a.txt": "a-kept", "b.txt": "b-kept"})
    reply = _call(root)
    assert reply["ok"] is False and "scratch/x.log" in reply["error"] and "record_halted_run_landed" in reply["error"]
    assert halted_run._read(record)["status"] == halted_run.STATUS_HALTED


def test_per_path_legacy_scratch_log_plus_evidence_paths_closes(legacy) -> None:
    root, record, _ = legacy
    halted_run.record_withheld(record, {"a.txt": "kept", "b.txt": "kept", "scratch/r/diag.log": "kept"})
    s1 = _commit(root, {"a.txt": "a-later"})
    s2 = _commit(root, {"b.txt": "b-later"})
    reply = _legacy_call(root, [s1, s2])
    assert reply["ok"] is True and reply["evidence"] == {"a.txt": s1, "b.txt": s2}
    assert reply["dispositions"]["scratch/r/diag.log"]["disposition"] == halted_run.DISPOSITION_SCRATCH
    marker = halted_run._read(record)
    assert marker[halted_run.PATH_DISPOSITIONS_FIELD]["a.txt"] == {"disposition": halted_run.DISPOSITION_SUPERSEDED, "sha": s1}
    assert not _indexed(root)


def test_per_path_legacy_without_scratch_spawns_one_cat_file(legacy, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, _ = legacy
    s = _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    _no_close_commit(monkeypatch)
    seen = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, args, *a, **kw) -> None:
            seen.append(list(args))
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    reply = _legacy_call(root, [s])
    monkeypatch.undo()
    assert reply["ok"] is True and sum("cat-file" in a for a in seen) == 1


@pytest.mark.parametrize("sneaky", ["scratch/../src/x.py", "scratch//x.log", "scratch/./x.log"])
def test_per_path_non_normalized_scratch_spelling_is_not_scratch(halted, sneaky) -> None:
    root, record, _ = halted
    _scratch_halted(root, record, {sneaky: "log"})
    _commit(root, {"a.txt": "a-later", "b.txt": "b-later"})
    reply = _call(root)
    assert reply["ok"] is False and sneaky in reply["error"]
    assert halted_run._read(record)["status"] == halted_run.STATUS_HALTED


def test_per_path_legacy_tracked_scratch_untouched_refuses(legacy) -> None:
    root, record, _ = legacy
    (root / "scratch").mkdir()
    _commit(root, {"scratch/t.log": "t"}, date=PRE_HALT)
    halted_run.record_withheld(record, {"a.txt": "kept", "scratch/t.log": "kept"})
    s1 = _commit(root, {"a.txt": "a-later"})
    reply = _legacy_call(root, [s1])
    assert reply["ok"] is False and "scratch/t.log" in reply["error"]
    assert halted_run._read(record)["status"] == halted_run.STATUS_HALTED


@pytest.fixture
def refused(tmp_path: Path) -> tuple[Path, Path]:
    """The example-stats-repo shape: manifest, run.json, run-stage.json, stage-receipt.json, stage-dirt.json; no halted.json."""
    _git(tmp_path, "init", "-q")
    record = tmp_path / ask_contract.RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True)
    for name in ("manifest.json", "run.json", "stage-receipt.json", "stage-dirt.json"):
        (record / name).write_text("{}", encoding="utf-8")
    ask_contract.write_run_stage_at(record, "staged", now=HALT_AT)
    return tmp_path, record


def test_refused_at_nested_chain_start_without_halted_json_closes(refused) -> None:
    root, record = refused
    reply = _call(root)
    assert reply["ok"] is True and reply["dispositions"] == {}
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_CLOSED
    assert ask_contract.read_run_stage(root, RUN)["stage"] == "halted"


def test_refused_at_stage_ok_close_lands_closure_commit(refused):
    root, record = refused
    _git(root, "add", "--", record.relative_to(root).as_posix())
    _git(root, "commit", "-q", "-m", "stage", date=PRE_HALT)
    reply = _call(root)
    assert reply["ok"] is True and reply["records_commit"] == _head(root)
    assert (record / halted_run.HALTED_FILE).relative_to(root).as_posix() in _git(
        root, "show", "--name-only", "--format=", "HEAD",
    ).splitlines()


@pytest.mark.parametrize("extra", ["kept.json", "withheld-prints.json", "run-baton.json"])
def test_refused_shape_with_work_record_still_refuses(refused, extra) -> None:
    root, record = refused
    (record / extra).write_text("{}", encoding="utf-8")
    assert _call(root)["ok"] is False
    assert not (record / halted_run.HALTED_FILE).exists()


def test_recently_active_unhalted_run_refuses_as_possibly_live(refused) -> None:
    root, record = refused
    ask_contract.write_run_stage_at(record, "staged")
    reply = _call(root)
    assert reply["ok"] is False and "live" in reply["error"]
    assert not (record / halted_run.HALTED_FILE).exists()


def test_unhalted_run_without_stage_receipt_refuses(refused) -> None:
    root, record = refused
    (record / "stage-receipt.json").unlink()
    assert _call(root)["ok"] is False


SIZING = "state/sizings/psz.yaml"
CHILD = "warp-psz-20261026T000000-abcdef"
PARENT = "ask-20261026T000000-0e64be"


def _halt_child(root, run_id, base, path: str = "a.txt"):
    record = root / ask_contract.RECORD_DIR_ROOT / run_id
    halted_run.write_halt_marker(record, halted=run_id, sizing_rel=SIZING, run_base_sha=base)
    ask_contract.write_run_stage_at(record, "halted", now=HALT_AT)
    halted_run.record_withheld(record, {path: "kept"})
    kept_work.write(record, {"c": {"paths": {path: _blob(root, "kept-" + run_id)}}}, None)
    return record


def _parent(root):
    """A staged parent ask of SIZING holding a live token and a live-run index row."""
    import json
    import time

    from coordinator_core.ops.dispatch_emit import live_run_writes, run_token

    record = root / ask_contract.RECORD_DIR_ROOT / PARENT
    record.mkdir(parents=True)
    (record / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    ask_contract.write_run_stage_at(record, "staged", now=HALT_AT)
    run_token.mint(root, PARENT, "scratch/w.mjs", session_id="s", sizing_rel=SIZING)
    live_run_writes.index_mint(root, PARENT, time.time())
    return record


def _parent_held(root) -> tuple:
    from coordinator_core.ops.dispatch_emit import live_run_index, run_token

    rows = live_run_index.read(root, 150.0)
    return run_token.is_live(root, PARENT), isinstance(rows, dict) and PARENT in rows


def test_closing_last_child_ends_parent_token_row_and_stage(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"a.txt": "a0"}, date="2026-01-01T00:00:00Z")
    _halt_child(tmp_path, CHILD, base)
    parent = _parent(tmp_path)
    assert _parent_held(tmp_path) == (True, True)
    _commit(tmp_path, {"a.txt": "a-later"})
    assert _call(tmp_path, CHILD)["ok"] is True
    assert _parent_held(tmp_path) == (False, False)
    assert ask_contract.read_run_stage(tmp_path, PARENT)["stage"] == "halted"
    assert parent.is_dir()


def test_closing_a_child_while_a_sibling_stays_halted_leaves_parent_live(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"a.txt": "a0", "b.txt": "b0"}, date="2026-01-01T00:00:00Z")
    _halt_child(tmp_path, CHILD, base)
    _halt_child(tmp_path, "warp-psz-20261026T000100-fedcba", base, path="b.txt")
    _parent(tmp_path)
    _commit(tmp_path, {"a.txt": "a-later"})
    assert _call(tmp_path, CHILD)["ok"] is True
    assert _parent_held(tmp_path) == (True, True)
    assert ask_contract.read_run_stage(tmp_path, PARENT)["stage"] == "staged"


def test_parent_of_another_sizing_is_untouched(tmp_path: Path) -> None:
    import json

    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"a.txt": "a0"}, date="2026-01-01T00:00:00Z")
    _halt_child(tmp_path, CHILD, base)
    record = _parent(tmp_path)
    (record / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/other.yaml"}), encoding="utf-8")
    _commit(tmp_path, {"a.txt": "a-later"})
    assert _call(tmp_path, CHILD)["ok"] is True
    assert _parent_held(tmp_path) == (True, True)


@pytest.fixture
def unstarted(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with a RUN halted dependents_unstarted that withholds nothing."""
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"a.txt": "a0"}, date="2026-01-01T00:00:00Z")
    record = tmp_path / ask_contract.RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(
        record, halted=ask_contract.HALT_DEPENDENTS_UNSTARTED, sizing_rel=None, run_base_sha=base, reason="C2 held on C1",
    )
    ask_contract.write_run_stage_at(record, "halted", now=HALT_AT)
    return tmp_path, record


def test_zero_withheld_dependents_unstarted_closes_superseded_naming_successor(unstarted) -> None:
    root, record = unstarted
    sha = _commit(root, {"a.txt": "a-later"})
    reply = op._handler({"run_id": RUN, "superseded_by": sha}, root)
    assert reply["ok"] is True and reply["evidence"] == {} and reply["dispositions"] == {} and reply["superseded_by"] == sha
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED and marker["superseded_by"] == sha
    assert marker[halted_run.SUPERSEDED_EVIDENCE_FIELD] == {} and marker[halted_run.PATH_DISPOSITIONS_FIELD] == {}
    assert not _indexed(root)


def test_zero_withheld_accepts_a_run_id_successor(unstarted) -> None:
    root, record = unstarted
    assert op._handler({"run_id": RUN, "superseded_by": "warp-y-20261116T000000-abcdef"}, root)["ok"] is True
    assert halted_run._read(record)["status"] == halted_run.STATUS_SUPERSEDED


@pytest.mark.parametrize("by", [None, "", "   ", "../x"])
def test_zero_withheld_without_a_named_successor_refuses(unstarted, by) -> None:
    root, record = unstarted
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = op._handler({"run_id": RUN, "superseded_by": by}, root)
    assert reply["ok"] is False and "superseded_by" in reply["error"]
    assert (record / halted_run.HALTED_FILE).read_text(encoding="utf-8") == before


def test_zero_withheld_live_successor_refuses(unstarted, monkeypatch: pytest.MonkeyPatch) -> None:
    root, record = unstarted
    from coordinator_core.ops.dispatch_emit import halted_run_landed

    monkeypatch.setattr(halted_run_landed, "_live_successor", lambda r, run: "warp-live-1")
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = op._handler({"run_id": RUN, "superseded_by": "warp-y-20261116T000000-abcdef"}, root)
    assert reply["ok"] is False and "warp-live-1" in reply["error"]
    assert (record / halted_run.HALTED_FILE).read_text(encoding="utf-8") == before


def test_zero_withheld_non_halted_run_refuses(unstarted) -> None:
    root, record = unstarted
    params = {"run_id": RUN, "superseded_by": "warp-y-20261116T000000-abcdef"}
    assert op._handler(params, root)["ok"] is True
    reply = op._handler(params, root)
    assert reply["ok"] is False and "superseded" in reply["error"]
