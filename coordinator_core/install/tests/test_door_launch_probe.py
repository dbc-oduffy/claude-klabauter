"""Launch probe and SAC classifier: every launch failure is injected, none depends on SAC being on."""

import subprocess
from pathlib import Path

from coordinator_core.install import door_launch_probe as dlp

IMAGE = Path("coordinator-invoke.exe")


def _winerror(code):
    err = OSError(13, "blocked")
    err.winerror = code
    return err


def _raising(exc):
    def runner(argv, timeout):
        raise exc

    return runner


def test_winerror_4551_is_sac_blocked_and_never_raises():
    ok, detail = dlp.probe_launch(IMAGE, runner=_raising(_winerror(4551)))
    assert ok is False
    assert "Smart App Control" in detail
    assert dlp.classify_launch_failure(exc=_winerror(4551)) == "sac-blocked"


def test_exit_126_permission_denied_is_sac_blocked():
    ok, detail = dlp.probe_launch(IMAGE, runner=lambda a, t: (126, "bash: x: Permission denied"))
    assert ok is False
    assert "Smart App Control" in detail
    assert dlp.classify_launch_failure(rc=126, output="Permission denied") == "sac-blocked"


def test_missing_image_is_not_found_not_sac():
    ok, detail = dlp.probe_launch(IMAGE, runner=_raising(FileNotFoundError(2, "gone")))
    assert ok is False
    assert "Smart App Control" not in detail
    assert dlp.classify_launch_failure(exc=FileNotFoundError()) == "not-found"
    assert dlp.classify_launch_failure(rc=127) == "not-found"
    assert dlp.classify_launch_failure(exc=_winerror(2)) == "not-found"


def test_unrelated_failures_class_other():
    assert dlp.classify_launch_failure(exc=OSError("boom")) == "other"
    assert dlp.classify_launch_failure(rc=126, output="") == "other"
    assert dlp.classify_launch_failure(rc=1, output="Permission denied") == "other"
    ok, detail = dlp.probe_launch(IMAGE, runner=_raising(OSError("boom")))
    assert ok is False and "Smart App Control" not in detail


def test_launching_image_passes_even_on_nonzero_exit():
    assert dlp.probe_launch(IMAGE, runner=lambda a, t: (0, ""))[0] is True
    assert dlp.probe_launch(IMAGE, runner=lambda a, t: (2, "usage"))[0] is True


def test_timeout_is_a_failure_not_a_raise():
    ok, detail = dlp.probe_launch(IMAGE, runner=_raising(subprocess.TimeoutExpired("x", 1)))
    assert ok is False and "did not answer" in detail


def test_probe_spawns_the_image_once_with_bounded_timeout():
    seen = []

    def runner(argv, timeout):
        seen.append((argv, timeout))
        return 0, ""

    dlp.probe_launch(IMAGE, runner=runner)
    assert len(seen) == 1
    assert seen[0][0][0] == str(IMAGE)
    assert 0 < seen[0][1] <= 30


def test_diagnosis_names_the_block_and_remedy():
    assert "Smart App Control" in dlp.SAC_DIAGNOSIS
    assert "4551" in dlp.SAC_DIAGNOSIS and "3118" in dlp.SAC_DIAGNOSIS
    assert "\n" not in dlp.SAC_DIAGNOSIS


def test_door_install_resolves_this_probe():
    from coordinator_core.install import door_install

    assert door_install._resolve_launch_probe() is dlp.probe_launch
