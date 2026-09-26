
import os
import shutil
import stat
import sys

import pytest

from coordinator_core.install import maximalist


@pytest.fixture
def orch():
    return maximalist._Orchestrator()


def _make_wrapper_src(tmp_path):
    src = tmp_path / "claude-doe.py"
    src.write_text("#!/usr/bin/env python3\nprint('claude-doe')\n")
    return str(src)


def test_windows_install_never_copy2s_onto_the_live_destination(tmp_path, orch, monkeypatch):
    monkeypatch.setattr(maximalist.os, "name", "nt")

    claude_klabauter_root = str(tmp_path / "claude-klabauter")
    coord_bin = os.path.join(claude_klabauter_root, "coordinator", "bin")
    os.makedirs(coord_bin, exist_ok=True)
    wrapper_src = os.path.join(coord_bin, "claude-doe.py")
    with open(wrapper_src, "w") as fh:
        fh.write("#!/usr/bin/env python3\nprint('claude-doe')\n")

    claude_home_dir = str(tmp_path / "home")
    wrapper_dst = os.path.join(claude_home_dir, ".local", "bin", "claude-doe")

    copy2_targets = []
    real_copy2 = shutil.copy2

    def _tracking_copy2(src, dst, *args, **kwargs):
        copy2_targets.append(dst)
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(maximalist.shutil, "copy2", _tracking_copy2)

    replace_calls = []
    real_replace = os.replace

    def _tracking_replace(src, dst, *args, **kwargs):
        replace_calls.append((src, dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(maximalist.os, "replace", _tracking_replace)

    maximalist._install_claude_doe_wrapper(
        coord_root=str(tmp_path / "doe-clone"),
        claude_home_dir=claude_home_dir,
        check_only=False,
        orch=orch,
        claude_klabauter_root=claude_klabauter_root,
        settings_bin=str(tmp_path / "settings-bin"),
    )

    assert copy2_targets, "shutil.copy2 was never called"
    for target in copy2_targets:
        assert target != wrapper_dst, (
            f"shutil.copy2 wrote directly to the live destination {wrapper_dst!r} "
            "-- must stage to a temp path instead"
        )
        assert os.path.dirname(target) == os.path.dirname(wrapper_dst), (
            "temp copy target must live in the same directory as wrapper_dst "
            "so os.replace stays a same-volume atomic rename"
        )

    assert replace_calls, "os.replace was never called to publish the wrapper"
    assert replace_calls[-1][1] == wrapper_dst

    assert os.path.isfile(wrapper_dst)
    with open(wrapper_dst) as fh:
        assert "claude-doe" in fh.read()
    if not sys.platform.startswith("win"):
        mode = os.stat(wrapper_dst).st_mode
        assert mode & stat.S_IXUSR

    leftover_tmp = [
        p for p in os.listdir(os.path.dirname(wrapper_dst))
        if p.startswith("claude-doe.tmp-")
    ]
    assert not leftover_tmp, f"temp publish file(s) left behind: {leftover_tmp}"


def test_windows_install_cleans_up_temp_on_copy_failure(tmp_path, orch, monkeypatch):
    monkeypatch.setattr(maximalist.os, "name", "nt")

    claude_klabauter_root = str(tmp_path / "claude-klabauter")
    coord_bin = os.path.join(claude_klabauter_root, "coordinator", "bin")
    os.makedirs(coord_bin, exist_ok=True)
    wrapper_src = os.path.join(coord_bin, "claude-doe.py")
    with open(wrapper_src, "w") as fh:
        fh.write("#!/usr/bin/env python3\nprint('claude-doe')\n")

    claude_home_dir = str(tmp_path / "home")
    wrapper_dst = os.path.join(claude_home_dir, ".local", "bin", "claude-doe")

    def _failing_copy2(src, dst, *args, **kwargs):
        raise OSError("simulated disk-full during staged copy")

    monkeypatch.setattr(maximalist.shutil, "copy2", _failing_copy2)

    with pytest.raises(SystemExit):
        maximalist._install_claude_doe_wrapper(
            coord_root=str(tmp_path / "doe-clone"),
            claude_home_dir=claude_home_dir,
            check_only=False,
            orch=orch,
            claude_klabauter_root=claude_klabauter_root,
            settings_bin=str(tmp_path / "settings-bin"),
        )

    assert not os.path.exists(wrapper_dst), (
        "destination must not exist after a failed staged copy"
    )
    local_bin = os.path.dirname(wrapper_dst)
    if os.path.isdir(local_bin):
        leftover_tmp = [
            p for p in os.listdir(local_bin) if p.startswith("claude-doe.tmp-")
        ]
        assert not leftover_tmp, f"temp publish file(s) left behind: {leftover_tmp}"
