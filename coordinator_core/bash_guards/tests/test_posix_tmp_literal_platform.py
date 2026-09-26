"""Tests for `_write_bump_applicability._posix_tmp_literal`'s platform branch.

WHY THIS IS ITS OWN FILE, and not another case in
`test_bump_outside_repo_write.py`: that module's autouse `_clean_bump_env`
fixture monkeypatches `_posix_tmp_literal` to a pytest `tmp_path`, for its own
isolation. That patch is exactly what hid the defect these tests pin -- the
real Windows behaviour of the seam was never once executed by the suite. A test
living in that module cannot assert the unpatched value, because the fixture
wins. So the seam gets a file with no such fixture.

THE DEFECT. `_all_temp_roots` appended the literal `"/tmp"` unconditionally, on
a stated assumption that "on Windows `/tmp` simply will not resolve to anything
meaningful". It does. `/tmp` is drive-RELATIVE on Windows, so
`os.path.realpath("/tmp")` resolves it against whatever drive the process is
cwd'd on. Every session running from a given drive therefore blessed that
drive's own top-level `\\tmp` as an always-allowed temp root, and
`bump_outside_repo_write` declined to deny writes landing there -- the observed
drive-root pollution, reproducing on each drive independently.
"""

from __future__ import annotations

import ntpath

from coordinator_core.bash_guards import _write_bump_applicability as applicability


def test_posix_tmp_literal_is_dropped_on_windows(monkeypatch):
    monkeypatch.setattr(applicability, "_host_is_windows", lambda: True)
    assert applicability._posix_tmp_literal() is None

    monkeypatch.setattr(applicability, "_host_is_windows", lambda: False)
    assert applicability._posix_tmp_literal() == "/tmp"


def test_drive_relative_tmp_is_not_an_allowed_temp_root_on_windows(monkeypatch):
    monkeypatch.setattr(applicability, "_host_is_windows", lambda: True)
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)

    drive_root_tmps = [
        root
        for root in applicability._all_temp_roots(env={})
        if ntpath.splitdrive(root)[1].strip("/\\").lower() == "tmp"
    ]
    assert drive_root_tmps == []


def test_posix_branch_still_contributes_the_tmp_literal(monkeypatch):
    monkeypatch.setattr(applicability, "_host_is_windows", lambda: False)
    assert applicability._posix_tmp_literal() == "/tmp"
