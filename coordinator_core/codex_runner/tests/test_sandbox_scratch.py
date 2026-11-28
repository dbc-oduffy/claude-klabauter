"""ensure_sandbox_scratch pre-creates <cwd>/scratch plainly and types a refused mkdir."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.codex_runner import sandbox_scratch as ss


def test_creates_scratch_and_warp_and_returns_scratch(tmp_path: Path) -> None:
    out = ss.ensure_sandbox_scratch(tmp_path)
    assert out == tmp_path / "scratch" and (out / "warp").is_dir()


def test_creates_the_state_warp_record_home(tmp_path: Path) -> None:
    ss.ensure_sandbox_scratch(tmp_path)
    assert (tmp_path / "state" / "warp").is_dir()


def test_a_ready_scratch_warp_does_not_skip_a_missing_state_warp(tmp_path: Path) -> None:
    (tmp_path / "scratch" / "warp").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    ss.ensure_sandbox_scratch(tmp_path)
    assert (tmp_path / "state" / "warp").is_dir()


def test_refused_state_warp_raises_typed_refusal_naming_that_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "scratch" / "warp").mkdir(parents=True)
    real = Path.mkdir

    def deny_state(self, *a, **kw):
        if self.name == "warp" and self.parent.name == "state":
            raise PermissionError(5, "Access is denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", deny_state)
    with pytest.raises(ss.ScratchGrantRefused) as ei:
        ss.ensure_sandbox_scratch(tmp_path)
    assert ei.value.path == tmp_path / "state" / "warp"


def test_ready_path_spawns_nothing_and_does_not_mkdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ss.ensure_sandbox_scratch(tmp_path)

    def boom(*a, **kw):
        raise AssertionError("spawned or mkdir on the ready path")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(Path, "mkdir", boom)
    assert ss.ensure_sandbox_scratch(tmp_path) == tmp_path / "scratch"


def test_refused_mkdir_raises_typed_refusal_naming_path_and_principal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(self, *a, **kw):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "mkdir", deny)
    with pytest.raises(ss.ScratchGrantRefused) as ei:
        ss.ensure_sandbox_scratch(tmp_path)
    exc = ei.value
    assert exc.path == tmp_path / "scratch"
    assert "CodexSandboxUsers" in exc.principal and "per-root capability SID" in exc.principal
    assert str(exc.path) in str(exc) and exc.principal in str(exc)
    assert isinstance(exc, RuntimeError)


def test_no_acl_or_process_machinery_is_imported() -> None:
    src = Path(ss.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in src and "icacls" not in src and "chmod" not in src


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL leg")
def test_windows_scratch_dacl_is_not_protected_and_parent_dacl_unchanged(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    def dacl_control(path: Path) -> int:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        sd = ctypes.c_void_p()
        rc = adv.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(sd))
        assert rc == 0
        ctrl, rev = wintypes.WORD(), wintypes.DWORD()
        assert adv.GetSecurityDescriptorControl(sd, ctypes.byref(ctrl), ctypes.byref(rev))
        ctypes.WinDLL("kernel32").LocalFree(sd)
        return ctrl.value

    def sddl(path: Path) -> str:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        sd = ctypes.c_void_p()
        assert adv.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(sd)) == 0
        s = wintypes.LPWSTR()
        assert adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, 4, ctypes.byref(s), None)
        text = s.value
        ctypes.WinDLL("kernel32").LocalFree(sd)
        return text

    before = sddl(tmp_path)
    scratch = ss.ensure_sandbox_scratch(tmp_path)
    assert not dacl_control(scratch) & 0x1000  # SE_DACL_PROTECTED
    assert sddl(tmp_path) == before
