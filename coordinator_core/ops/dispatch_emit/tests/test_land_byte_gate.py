"""land_byte_gate.admit serialises landings across processes and reports the admission instant."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import land_byte_gate
from coordinator_core.ops.dispatch_emit.land_byte_gate import LandAdmissionTimeout, admit

_PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_HOLDER = textwrap.dedent("""\
    import sys, time
    sys.path.insert(0, sys.argv[2])
    from pathlib import Path
    from coordinator_core.ops.dispatch_emit.land_byte_gate import admit

    repo = Path(sys.argv[1])
    with admit(repo, 10, holder_label=sys.argv[4] if len(sys.argv) > 4 else "holder", anchor_root=repo):
        sys.stdout.write("LOCKED\\n")
        sys.stdout.flush()
        time.sleep(float(sys.argv[3]))
""")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), capture_output=True, check=True,
                   creationflags=_NO_WINDOW)
    return root


def _start_holder(tmp_path: Path, repo: Path, hold_secs: float, label: str = "holder") -> subprocess.Popen:
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(repo), _PROJECT_ROOT, str(hold_secs), label],
        stdout=subprocess.PIPE,
        creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
    )
    assert proc.stdout.readline().strip() == b"LOCKED"
    return proc


def test_uncontended_admit_yields_the_admission_instant(tmp_path):
    repo = _repo(tmp_path)
    before = time.monotonic()
    with admit(repo, 1, anchor_root=repo) as admitted_at:
        assert before <= admitted_at <= time.monotonic()


def test_second_process_times_out_before_the_block_runs(tmp_path):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 5)
    try:
        ran = []
        with pytest.raises(LandAdmissionTimeout):
            with admit(repo, 0.3, anchor_root=repo):
                ran.append(1)  # pragma: no cover
        assert ran == []
    finally:
        proc.kill()
        proc.wait()


def test_second_process_is_admitted_after_the_first_releases(tmp_path):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 1.0)
    released_after = time.monotonic() + 0.5
    try:
        with admit(repo, 10, anchor_root=repo) as admitted_at:
            assert admitted_at >= released_after
    finally:
        proc.kill()
        proc.wait()


def test_nested_admit_in_one_context_is_a_no_op_with_the_outer_instant(tmp_path):
    repo = _repo(tmp_path)
    with admit(repo, 1, anchor_root=repo) as outer:
        with admit(repo, 0.1, anchor_root=repo) as inner:
            assert inner == outer


def test_gate_is_released_after_the_block(tmp_path):
    repo = _repo(tmp_path)
    with admit(repo, 1, anchor_root=repo):
        pass
    with admit(repo, 0.5, anchor_root=repo):
        pass


def test_same_holder_run_in_another_process_is_admitted_at_once(tmp_path):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 5, label="run-1")
    try:
        started = time.monotonic()
        with admit(repo, 10, holder_label="run-1", anchor_root=repo):
            pass
        assert time.monotonic() - started < 2.0
        with pytest.raises(LandAdmissionTimeout):  # the nested exit did not release the holder's lock
            with admit(repo, 0.3, holder_label="run-2", anchor_root=repo):
                pass  # pragma: no cover
    finally:
        proc.kill()
        proc.wait()


def test_same_holder_run_in_another_thread_is_admitted_at_once_and_does_not_release(tmp_path):
    repo = _repo(tmp_path)
    inner_secs = []
    with admit(repo, 1, holder_label="run-1", anchor_root=repo):
        def leg():
            started = time.monotonic()
            with admit(repo, 10, holder_label="run-1", anchor_root=repo):
                pass
            inner_secs.append(time.monotonic() - started)
            try:
                with admit(repo, 0.2, holder_label="run-2", anchor_root=repo):
                    inner_secs.append("admitted")  # pragma: no cover
            except LandAdmissionTimeout:
                inner_secs.append("queued")

        t = threading.Thread(target=leg)
        t.start()
        t.join(15)
    assert inner_secs[0] < 2.0 and inner_secs[1] == "queued"


def test_different_live_holder_still_queues(tmp_path):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 5, label="run-1")
    try:
        with pytest.raises(LandAdmissionTimeout):
            with admit(repo, 0.3, holder_label="run-2", anchor_root=repo):
                pass  # pragma: no cover
    finally:
        proc.kill()
        proc.wait()


def test_empty_holder_label_never_matches_an_empty_recorded_label(tmp_path):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 5, label="")
    try:
        with pytest.raises(LandAdmissionTimeout):
            with admit(repo, 0.3, anchor_root=repo):
                pass  # pragma: no cover
    finally:
        proc.kill()
        proc.wait()


def test_dead_pid_holder_is_reclaimed_at_once(tmp_path):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 30, label="run-1")
    proc.kill()
    proc.wait()
    started = time.monotonic()
    with admit(repo, 10, holder_label="run-2", anchor_root=repo):
        pass
    assert time.monotonic() - started < 2.0


def test_no_caller_wait_queues_behind_a_live_holder_past_the_default(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(land_byte_gate, "DEFAULT_WAIT_SECS", 0.3)
    proc = _start_holder(tmp_path, repo, 1.5, label="run-1")
    try:
        started = time.monotonic()
        with admit(repo, holder_label="run-2", anchor_root=repo) as admitted_at:
            assert admitted_at - started > 0.6  # outlived the 0.3s default; no timeout
    finally:
        proc.kill()
        proc.wait()


def test_no_caller_wait_reclaims_a_dead_holder(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(land_byte_gate, "DEFAULT_WAIT_SECS", 0.3)
    proc = _start_holder(tmp_path, repo, 30, label="run-1")
    proc.kill()
    proc.wait()
    started = time.monotonic()
    with admit(repo, holder_label="run-2", anchor_root=repo):
        pass
    assert time.monotonic() - started < 2.0


def test_no_caller_wait_follow_is_bounded_by_the_dispatcher_clamp(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 30, label="run-1")
    deadline = time.monotonic() + dispatch_abandon.REPLY_MARGIN_SECS + 0.6
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: deadline - time.monotonic())
    try:
        started = time.monotonic()
        with pytest.raises(LandAdmissionTimeout):
            with admit(repo, holder_label="run-2", anchor_root=repo):
                pass  # pragma: no cover
        assert time.monotonic() - started < 3.0
    finally:
        proc.kill()
        proc.wait()


def test_explicit_wait_is_still_a_fixed_bound_against_a_live_holder(tmp_path):
    repo = _repo(tmp_path)
    proc = _start_holder(tmp_path, repo, 5, label="run-1")
    try:
        with pytest.raises(LandAdmissionTimeout):
            with admit(repo, 0.3, holder_label="run-2", anchor_root=repo):
                pass  # pragma: no cover
    finally:
        proc.kill()
        proc.wait()


def test_wait_is_clamped_to_the_dispatcher_remaining_less_the_reply_margin(monkeypatch):
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: dispatch_abandon.REPLY_MARGIN_SECS + 1.5)
    assert land_byte_gate._wait_budget(60) == pytest.approx(1.5)
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: 0.5)
    assert land_byte_gate._wait_budget(60) == 0.0


def test_unbound_dispatch_uses_the_caller_wait_then_the_default(monkeypatch):
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: None)
    assert land_byte_gate._wait_budget(7) == 7.0
    assert land_byte_gate._wait_budget(None) == land_byte_gate.DEFAULT_WAIT_SECS
