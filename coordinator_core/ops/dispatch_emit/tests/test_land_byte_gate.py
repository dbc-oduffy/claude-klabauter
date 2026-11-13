"""land_byte_gate.admit serialises landings across processes and reports the admission instant."""

from __future__ import annotations

import subprocess
import sys
import textwrap
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
    with admit(repo, 10, holder_label="holder", anchor_root=repo):
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


def _start_holder(tmp_path: Path, repo: Path, hold_secs: float) -> subprocess.Popen:
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(repo), _PROJECT_ROOT, str(hold_secs)],
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


def test_wait_is_clamped_to_the_dispatcher_remaining_less_the_reply_margin(monkeypatch):
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: dispatch_abandon.REPLY_MARGIN_SECS + 1.5)
    assert land_byte_gate._wait_budget(60) == pytest.approx(1.5)
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: 0.5)
    assert land_byte_gate._wait_budget(60) == 0.0


def test_unbound_dispatch_uses_the_caller_wait_then_the_default(monkeypatch):
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: None)
    assert land_byte_gate._wait_budget(7) == 7.0
    assert land_byte_gate._wait_budget(None) == land_byte_gate.DEFAULT_WAIT_SECS
