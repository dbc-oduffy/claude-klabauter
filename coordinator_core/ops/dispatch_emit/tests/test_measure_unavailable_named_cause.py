"""A measured path that cannot hash carries a named cause at every rendering site; the CR-under-`text=auto`
shape whose index blob is unreadable (run 12962a) is decided by its two candidate blobs, so a green run lands."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.git import commit
from coordinator_core.git.git_state import IndexParseError
from coordinator_core.ops.dispatch_emit import land_run, measured_rev as mr, requirements_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_RECORD

REL = "tests/test_kill.py"
LF = b"def test_a():\n    assert True\n"
CRLF = LF.replace(b"\n", b"\r\n")
VERDICT = {"items": [{"id": "R1", "status": "met"}]}
LEG_ERROR = {"wave_not_run": [{"agent_type": "dispatch.test_leg", "reason": "returned no review"}]}
GREEN = {"status_before": "pass", "status_after": "pass", "failing": [], "tests_run": 88}
RED = {"status_before": "fail", "status_after": "fail", "failing": ["t::x"], "tests_run": 88}


def _repo(tmp_path: Path, body: bytes, record_body: bytes | None = None) -> Path:
    """A `* text=auto eol=lf` tree holding `body` at REL; the run record holds `record_body`'s check-in blob."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitattributes").write_text("* text=auto eol=lf\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / REL).write_bytes(body)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    recorded = commit._blob_sha1(record_body if record_body is not None else LF)
    (run_dir / MEASURED_REV_RECORD).write_text(json.dumps({"blobs": {REL: recorded}, "removed": []}))
    return run_dir


def _index_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(root: Path, rel: str) -> str:
        raise IndexParseError("index busy")

    monkeypatch.setattr(commit, "_index_entry_sha", boom)


def _stall(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    release = threading.Event()
    monkeypatch.setattr(mr, "REREAD_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(commit, "CHECKIN_READ_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(commit, "_checkin_blob_sha_read", lambda root, rel: release.wait(5) and None)
    monkeypatch.setattr(commit, "diagnose_checkin_blob", lambda root, rel: release.wait(5) and None)
    return release


def _gate(root: Path, monkeypatch: pytest.MonkeyPatch, run_dir: Path, review: dict) -> dict | None:
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda r, d: SimpleNamespace(items=[1], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda r, d: [])
    monkeypatch.setattr(requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []})
    return land_run._ledger_gate(root, run_dir, VERDICT, None, [], {}, None, review, None, None)[0]


def _terminal(root: Path, run_dir: Path, review: dict) -> dict | None:
    ledger = SimpleNamespace(items=[1], all_of_pm_ask=False)
    return refusals._measured_rev_drift_refusal(run_dir, root, VERDICT, ledger, review)


@pytest.mark.parametrize("body", [LF, CRLF])
def test_a_passing_test_file_measures_clean_and_the_gate_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    run_dir = _repo(tmp_path, body)
    assert mr.measured_rev_drift(run_dir, tmp_path) == []
    assert _gate(tmp_path, monkeypatch, run_dir, {"wave_reviews": []}) is None


@pytest.mark.parametrize("body", [LF, CRLF])
def test_index_unreadable_text_auto_path_matches_by_candidate_blob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    run_dir = _repo(tmp_path, body)
    _index_unreadable(monkeypatch)
    assert mr.measured_rev_drift(run_dir, tmp_path) == []


def test_index_unreadable_text_auto_path_with_changed_bytes_is_drift_not_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _repo(tmp_path, CRLF.replace(b"True", b"False"))
    _index_unreadable(monkeypatch)
    assert mr.measured_rev_drift(run_dir, tmp_path) == [REL]


def test_the_12962a_shape_lands_at_default_arguments_on_a_green_reverify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _repo(tmp_path, CRLF)
    _index_unreadable(monkeypatch)
    review = {**LEG_ERROR, "reverify": GREEN}
    assert _gate(tmp_path, monkeypatch, run_dir, review) is None
    assert _terminal(tmp_path, run_dir, review) is None


def test_a_green_reverify_supersedes_a_measure_unavailable_the_leg_left_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _repo(tmp_path, LF)
    release = _stall(monkeypatch)
    try:
        review = {**LEG_ERROR, "reverify": GREEN}
        assert _gate(tmp_path, monkeypatch, run_dir, review) is None
        assert _terminal(tmp_path, run_dir, review) is None
    finally:
        release.set()


def test_a_stalled_read_refuses_naming_path_and_cause_at_every_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _repo(tmp_path, LF)
    release = _stall(monkeypatch)
    try:
        with pytest.raises(mr.MeasureUnavailable) as exc:
            mr.measured_rev_drift(run_dir, tmp_path)
        want = f"measure_unavailable: {REL} (read stalled past the 0.05s and 0.05s bounds)"
        assert exc.value.reason() == want
        assert (mr.remeasure_reason(run_dir, tmp_path) or "").startswith(f"measure_unavailable: {REL} (read stalled")

        for review in ({**LEG_ERROR, "reverify": RED}, LEG_ERROR):
            gate = _gate(tmp_path, monkeypatch, run_dir, review)
            assert gate["refused"] == "test-infra"
            assert f"{REL} (read stalled past" in gate["test_infra"]["reason"]
            term = _terminal(tmp_path, run_dir, review)
            assert term["refused"] == "test-infra" and f"{REL} (read stalled past" in term["test_infra"]["reason"]
    finally:
        release.set()


READ_TAKES = 0.4  # simulated duration of the CR-path read: past the first bound, inside the wider one


def _read_that_takes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Simulate a re-read needing `READ_TAKES` seconds without sleeping: the bounded runner answers its
    default when the deadline it is handed is shorter (spending that deadline on a fake clock `mr` reads),
    runs the callable otherwise. Records (kind, deadline)."""
    from coordinator_core import bounded_io

    real, seen, now = bounded_io.run_bounded, [], [0.0]

    def fake(key, fn, *, deadline, default):
        if not key.startswith("measured_rev_"):
            return real(key, fn, deadline=deadline, default=default)
        seen.append((key.split(":")[0], deadline))
        if deadline < READ_TAKES:
            now[0] += deadline
            return default
        return fn()

    monkeypatch.setattr(bounded_io, "run_bounded", fake)
    monkeypatch.setattr(mr, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda root, rel: None)
    return seen


def test_a_read_between_the_first_and_the_remeasure_bound_is_clean_only_on_remeasure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert mr.REREAD_DEADLINE_SECS < READ_TAKES < mr.REMEASURE_DEADLINE_SECS
    run_dir = _repo(tmp_path, LF)
    seen = _read_that_takes(monkeypatch)
    with pytest.raises(mr.MeasureUnavailable):
        mr.measured_rev_drift(run_dir, tmp_path)
    assert seen[0] == ("measured_rev_reread", mr.REREAD_DEADLINE_SECS)
    seen.clear()
    assert mr.remeasure_reason(run_dir, tmp_path) is None
    assert seen == [("measured_rev_reread", mr.REMEASURE_DEADLINE_SECS)]


def test_a_diagnosing_read_that_hashed_decides_the_path_by_its_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = _repo(tmp_path, LF)
    monkeypatch.setattr(commit, "checkin_blob_sha", lambda root, rel: None)
    monkeypatch.setattr(mr, "_reread_bounded", lambda root, rel, wait: None)
    monkeypatch.setattr(mr, "_diagnose_bounded", lambda root, rel, wait, total: commit.BlobDiagnosis(commit._blob_sha1(LF), "answered"))
    assert mr.measured_rev_drift(clean, tmp_path) == []
    monkeypatch.setattr(mr, "_diagnose_bounded", lambda root, rel, wait, total: commit.BlobDiagnosis(commit._blob_sha1(b"other\n"), "answered"))
    assert mr.measured_rev_drift(clean, tmp_path) == [REL]


def test_the_reread_and_the_diagnose_of_one_path_share_the_remeasure_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _repo(tmp_path, LF)
    seen = _read_that_takes(monkeypatch)
    monkeypatch.setattr(mr, "REMEASURE_DEADLINE_SECS", 0.2)
    reason = mr.remeasure_reason(run_dir, tmp_path)
    assert (reason or "").startswith(f"measure_unavailable: {REL} (read stalled past")
    assert [kind for kind, _ in seen] == ["measured_rev_reread", "measured_rev_diagnose"]
    assert sum(deadline for _, deadline in seen) <= mr.REMEASURE_DEADLINE_SECS + 0.05


def _fake_clock() -> tuple:
    now = [0.0]

    def sleep(secs: float) -> None:
        now[0] += secs

    return (lambda: now[0]), sleep, now


def test_read_drain_returns_once_the_stalled_read_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import bounded_io

    answers = iter([True, True, False])
    monkeypatch.setattr(bounded_io, "is_stalled", lambda key: next(answers))
    clock, sleep, now = _fake_clock()
    land_run._await_read_drain("x/test-leg.json", clock=clock, sleep=sleep)
    assert now[0] == pytest.approx(0.02) and now[0] < land_run.LEG_STALL_DRAIN_SECS


def test_read_drain_gives_up_at_its_dial_for_a_wedged_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import bounded_io

    keys = []
    monkeypatch.setattr(bounded_io, "is_stalled", lambda key: keys.append(key) or True)
    clock, sleep, now = _fake_clock()
    land_run._await_read_drain("x/test-leg.json", clock=clock, sleep=sleep)
    assert land_run.LEG_STALL_DRAIN_SECS <= now[0] < land_run.LEG_STALL_DRAIN_SECS + 0.05
    assert set(keys) == {"read:x/test-leg.json"}


def test_is_stalled_reports_the_overrun_worker_until_it_returns() -> None:
    from coordinator_core import bounded_io

    key = "is-stalled-probe"
    release = threading.Event()
    assert bounded_io.is_stalled(key) is False
    assert bounded_io.run_bounded(key, release.wait, deadline=0.02, default="stalled") == "stalled"
    job = bounded_io._stalled[key]
    try:
        assert bounded_io.is_stalled(key) is True
    finally:
        release.set()
        assert job.done.wait(5)
    assert bounded_io.is_stalled(key) is False


def test_the_halted_dispatch_drains_and_retries_a_test_leg_stall_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.bounded_io import IoIndeterminate
    from coordinator_core.git import git_state

    path = str(tmp_path / "test-leg.json")
    calls, drains = [], []

    def mode(*args):
        calls.append(args)
        if len(calls) <= stalls:
            raise IoIndeterminate(path, 5)
        return {"landed": True}

    monkeypatch.setattr(git_state, "head_sha", lambda root, fresh=False: "h1")
    monkeypatch.setattr(land_run, "_head_after_stall", lambda root: "h1")
    monkeypatch.setattr(land_run, "_warm_test_leg_checkin", lambda root, params: None)
    monkeypatch.setattr(land_run, "_dispatch_mode", mode)
    monkeypatch.setattr(land_run, "_await_read_drain", drains.append)
    monkeypatch.setattr(land_run, "_halt_read_reply", lambda root, params, exc, head, started: {"refused": land_run.HALT_READ_REFUSED})

    stalls = 1
    assert land_run._dispatch_halted_bounded(tmp_path, tmp_path, {}) == {"landed": True}
    assert drains == [path] and len(calls) == 2

    calls.clear(), drains.clear()
    stalls = 2
    assert land_run._dispatch_halted_bounded(tmp_path, tmp_path, {}) == {"refused": land_run.HALT_READ_REFUSED}
    assert drains == [path] and len(calls) == 2


def test_a_non_regular_path_names_its_cause(tmp_path: Path) -> None:
    run_dir = _repo(tmp_path, LF)
    (tmp_path / REL).unlink()
    (tmp_path / REL).mkdir()
    with pytest.raises(mr.MeasureUnavailable) as exc:
        mr.measured_rev_drift(run_dir, tmp_path)
    assert exc.value.causes == {REL: "not a regular file (a read of it could block)"}


def test_a_filter_driver_path_names_its_cause(tmp_path: Path) -> None:
    run_dir = _repo(tmp_path, LF)
    (tmp_path / ".gitattributes").write_text(f"{REL} filter=lfs\n")
    with pytest.raises(mr.MeasureUnavailable) as exc:
        mr.measured_rev_drift(run_dir, tmp_path)
    assert exc.value.causes == {REL: "a filter.*.clean driver may apply to this path"}


def test_diagnose_names_the_two_candidate_blobs_when_the_index_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path, CRLF)
    _index_unreadable(monkeypatch)
    diag = commit.diagnose_checkin_blob(tmp_path, REL)
    assert diag.sha is None
    assert diag.alt_shas == (commit._blob_sha1(CRLF), commit._blob_sha1(LF))
    assert diag.cause.endswith("the git index is unreadable (index busy)")


def test_the_undecided_cause_detail_survives_a_colon_in_the_path() -> None:
    exc = commit.IndexFormUndecided("a: b.py: the git index is unreadable (x)", (b"",), "the git index is unreadable (x)")
    assert exc.detail == "the git index is unreadable (x)"
    assert isinstance(exc, commit.FilterUnsupported)


def test_an_unclassified_cause_is_never_a_bare_path() -> None:
    exc = mr.MeasureUnavailable(["a.py", "b.py"], {"a.py": "why"})
    assert exc.reason() == f"measure_unavailable: a.py (why), b.py ({mr.UNNAMED_CAUSE})"
    assert exc.paths == ["a.py", "b.py"] and isinstance(exc, OSError)


def test_a_spent_total_budget_makes_the_path_unavailable_with_that_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _repo(tmp_path, LF)
    release = _stall(monkeypatch)
    monkeypatch.setattr(mr, "MEASURE_TOTAL_BUDGET_SECS", 0.0)
    try:
        with pytest.raises(mr.MeasureUnavailable) as exc:
            mr.measured_rev_drift(run_dir, tmp_path)
        assert "budget" in exc.value.causes[REL]
    finally:
        release.set()
