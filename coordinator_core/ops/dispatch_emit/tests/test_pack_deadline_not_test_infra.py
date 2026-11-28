"""A verify leg whose only setup error is a pack-read deadline is `pack-read-stall` (a mechanical refire), never
`test_infra_failure`; any other all-setup-error run keeps `test_infra_failure`."""

from __future__ import annotations

import pytest

from coordinator_core.git.git_objects import PACK_READ_STALLED_ADVISORY
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import READER, _fake_pytest, _repo

pytestmark = [pytest.mark.complete]


def _setup_errors(exc_line: str) -> str:
    return (
        "==== ERRORS ====\n____ ERROR at setup of test_x ____\n\n    def fix(tmp_path):\n"
        f"E       {exc_line}\n\nt/test_reader.py:3: Error\n"
        "==== short test summary info ====\nERROR t/test_reader.py::test_x - setup failed\n"
        "==== 248 errors in 7.78s ====\n"
    )


_DEADLINE = _setup_errors(
    f"coordinator_core.git.git_objects.PackReadDeadline: {PACK_READ_STALLED_ADVISORY}: pack read under C:/r/.git exceeded 4.0s"
)
_OTHER = _setup_errors("PermissionError: [WinError 5] Access is denied: 'pytest-of-u'")


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def test_zero_run_cause_types_a_pack_deadline_setup_error_and_keeps_every_other():
    assert cov.zero_run_cause("E   PackReadDeadline: read budget spent") == hrc.PACK_READ_STALL_HALT_KIND
    assert cov.zero_run_cause(f"E   RuntimeError: {PACK_READ_STALLED_ADVISORY}") == hrc.PACK_READ_STALL_HALT_KIND
    assert cov.zero_run_cause("E   PermissionError: [WinError 5]") == cov.ZERO_RUN_TEST_INFRA == "test_infra_failure"
    assert cov.zero_run_cause("") == cov.ZERO_RUN_TEST_INFRA


@pytest.mark.spawns_process
def test_a_zero_test_verify_whose_setup_error_is_a_pack_deadline_is_pack_read_stall(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    _fake_pytest(monkeypatch, 1, _DEADLINE)

    out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "red", out
    assert out.cause == hrc.PACK_READ_STALL_HALT_KIND == "pack-read-stall", out
    assert out.cause != cov.ZERO_RUN_TEST_INFRA
    assert "PackReadDeadline" in out.detail


@pytest.mark.spawns_process
def test_a_zero_test_verify_with_any_other_setup_error_stays_test_infra_failure(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    _fake_pytest(monkeypatch, 1, _OTHER)

    out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "red" and out.cause == cov.ZERO_RUN_TEST_INFRA, out
