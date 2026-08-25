"""
coordinator_core.install.tests.test_claude_doe_wrapper_windows_publish

Structural coverage for `_install_claude_doe_wrapper`'s Windows (`os.name ==
"nt"`) branch in coordinator_core/install/maximalist.py: the wrapper publish
must be staged to a same-directory temp path and moved into place with
`os.replace`, never written in place onto the live destination via
`shutil.copy2(wrapper_src, wrapper_dst)` directly.

Why this matters: `wrapper_dst` (`~/.local/bin/claude-doe`) is a launcher a
concurrent shell may already be running. An in-place `shutil.copy2` onto that
path truncates-then-writes it, so a concurrent reader can observe or execute
a half-written file mid-copy. The POSIX branch a few lines below already
guards this with a tmp-then-`os.replace` publish; this test proves the
Windows branch now does too.

Cannot execute the real Windows code path on this host (`os.name` is not
settable at the OS level) -- this forces the `nt` branch by monkeypatching
`os.name` and exercises the actual function body, verifying via a wrapped
`shutil.copy2` that it is never called with `wrapper_dst` as its destination,
and that the destination is only ever reached via `os.replace`. This proves
the call-sequence/no-in-place-write contract; live Windows execution (real
`MoveFileExW` semantics, real sharing-violation behavior) remains unverified
without a Windows host.
"""

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
    """`shutil.copy2` must never target `wrapper_dst` directly -- only a
    same-directory temp path, published afterward via `os.replace`."""
    monkeypatch.setattr(maximalist.os, "name", "nt")

    makima_root = str(tmp_path / "makima")
    coord_bin = os.path.join(makima_root, "coordinator", "bin")
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
        makima_root=makima_root,
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
    # Exec-bit only where the concept exists. On Windows st_mode carries no
    # real permission bits — it is synthesised from the read-only attribute,
    # so S_IXUSR is absent on a perfectly good published wrapper and this
    # assertion fails on the very platform whose branch the module exercises.
    # No Windows-side invariant currently stands in for the skipped
    # assertion (wrapper invocability there is governed by extension/shebang
    # association, not the exec bit); this test does not verify it.
    #
    # Review: coordinator:code-reviewer (P1/P2) -- must NOT guard on
    # `os.name` here: the monkeypatch above (`maximalist.os` is the same
    # singleton `os` module this test also imports) already forced
    # `os.name == "nt"` process-wide for the rest of this test body, so an
    # `if os.name != "nt":` guard is always false regardless of the real
    # host and silently skips the exec-bit assertion everywhere, including
    # on POSIX where it is the assertion that matters. `sys.platform` is
    # never patched anywhere in this module, so it reflects the real host.
    if not sys.platform.startswith("win"):
        mode = os.stat(wrapper_dst).st_mode
        assert mode & stat.S_IXUSR

    leftover_tmp = [
        p for p in os.listdir(os.path.dirname(wrapper_dst))
        if p.startswith("claude-doe.tmp-")
    ]
    assert not leftover_tmp, f"temp publish file(s) left behind: {leftover_tmp}"


def test_windows_install_cleans_up_temp_on_copy_failure(tmp_path, orch, monkeypatch):
    """A failed `shutil.copy2` into the temp path must not leave a stray
    temp artifact, and must exit loudly (SystemExit) rather than silently
    leaving a half-published wrapper."""
    monkeypatch.setattr(maximalist.os, "name", "nt")

    makima_root = str(tmp_path / "makima")
    coord_bin = os.path.join(makima_root, "coordinator", "bin")
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
            makima_root=makima_root,
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
