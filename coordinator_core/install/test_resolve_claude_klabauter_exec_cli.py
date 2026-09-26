from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
)

_spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_under_test_exec_cli", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
resolve_claude_klabauter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = resolve_claude_klabauter
_spec.loader.exec_module(resolve_claude_klabauter)

_FIXTURE_TARGET_NAME = "fixture-cli"

#: enumerates its subjects in `COLD_PATH_MODULES` — and `_resolve_claude_klabauter.py`
_REMEDIATION_TEXT = "run python3 <engine-clone>/scripts/setup.py"


class _OSNameProxy:

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "name", name)

    def __getattr__(self, attr):
        return getattr(os, attr)


def _patch_os_name(monkeypatch, name: str) -> "_OSNameProxy":
    proxy = _OSNameProxy(name)
    monkeypatch.setattr(resolve_claude_klabauter, "os", proxy)
    return proxy


def _patch_root_resolution(monkeypatch, bin_dir: Path) -> None:
    monkeypatch.setattr(
        resolve_claude_klabauter,
        "resolve_claude_klabauter_root_with_class",
        lambda: (str(bin_dir), resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE),
    )
    monkeypatch.setattr(resolve_claude_klabauter, "_validate_bin_dir", lambda root: root)
    monkeypatch.setattr(resolve_claude_klabauter, "resolve_claude_klabauter_bin_dir", lambda: str(bin_dir))


def _stub_bin_dir(monkeypatch, bin_dir: Path, target_name: str, *, body: str = "", create_target: bool = True) -> str:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if create_target:
        (bin_dir / target_name).write_text(body or "#!/usr/bin/env python3\n", encoding="utf-8")
    _patch_root_resolution(monkeypatch, bin_dir)
    return str(bin_dir) + "/" + target_name


def _write_sentinel(coord_bin: Path) -> None:
    sentinel = coord_bin / "archive-stamp-cli"
    sentinel.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    sentinel.chmod(0o755)
    if os.name == "nt":
        # PATHEXT-based, not stat-mode-based (NTFS has no exec bit for
        (coord_bin / "archive-stamp-cli.cmd").write_text("@echo SENTINEL\r\n", encoding="utf-8")


def _write_forwarder(bin_dir: Path, forwarder_name: str, target: str) -> Path:
    content = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        "from _resolve_claude_klabauter import exec_cli  # noqa: E402\n"
        f"exec_cli({target!r})\n"
    )
    forwarder_path = bin_dir / forwarder_name
    forwarder_path.write_text(content, encoding="utf-8")
    forwarder_path.chmod(0o755)
    return forwarder_path


@dataclass
class _ExecResult:
    returncode: int
    stdout: str
    stderr: str


def _invoke_posix_subprocess(
    tmp_path: Path,
    argv: List[str],
    *,
    target_body: Optional[str],
    resolution_ok: bool,
    target_mode: int = 0o755,
) -> _ExecResult:
    """Generate a real forwarder into a tmp settings-home pointed (via
    ``COORDINATOR_SETTINGS_HOME`` + ``registry.local.toml``) at a tmp claude-klabauter
    root, and invoke it as a genuinely separate process."""
    settings_home = tmp_path / "settings-home"
    bin_dir = settings_home / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(_MODULE_PATH, bin_dir / "_resolve_claude_klabauter.py")
    forwarder_path = _write_forwarder(bin_dir, "fwd-under-test", _FIXTURE_TARGET_NAME)

    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    if resolution_ok:
        claude_klabauter_root = tmp_path / "claude-klabauter-live-root"
        coord_bin = claude_klabauter_root / "coordinator" / "bin"
        coord_bin.mkdir(parents=True)
        _write_sentinel(coord_bin)
        if target_body is not None:
            target_path = coord_bin / _FIXTURE_TARGET_NAME
            target_path.write_text(target_body, encoding="utf-8")
            target_path.chmod(target_mode)
        (ml_dir / "registry.local.toml").write_text(
            f"[repos]\nclaude_klabauter = '{claude_klabauter_root}'\n", encoding="utf-8"
        )

    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = str(settings_home)
    # Rung 0 of `_resolve_claude_klabauter_root`'s ladder reads COORDINATOR_ENGINE_ROOT
    # up. MACHINE_LOCAL_REGISTRY_DIR would similarly bypass the tmp
    # read ahead of COORDINATOR_SETTINGS_HOME); stripped for the same
    env.pop("COORDINATOR_ENGINE_ROOT", None)
    env.pop("MACHINE_LOCAL_REGISTRY_DIR", None)

    result = subprocess.run(
        [sys.executable, str(forwarder_path), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        **no_console_creationflags(),
    )
    return _ExecResult(result.returncode, result.stdout, result.stderr)


def _invoke_nt_inprocess(
    tmp_path: Path,
    monkeypatch,
    capsys,
    argv: List[str],
    *,
    target_body: Optional[str],
    resolution_ok: bool,
) -> _ExecResult:
    bin_dir = tmp_path / "nt-coordinator-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    if resolution_ok:
        _patch_root_resolution(monkeypatch, bin_dir)
        if target_body is not None:
            (bin_dir / _FIXTURE_TARGET_NAME).write_text(target_body, encoding="utf-8")
    else:
        def _raise() -> str:
            raise resolve_claude_klabauter.ClaudeKlabauterResolutionError("ERROR: boom\n")

        monkeypatch.setattr(resolve_claude_klabauter, "resolve_claude_klabauter_root_with_class", _raise)

    _patch_os_name(monkeypatch, "nt")

    original_argv = list(sys.argv)
    with pytest.raises(SystemExit) as excinfo:
        resolve_claude_klabauter.exec_cli(_FIXTURE_TARGET_NAME, argv=argv)
    assert sys.argv == original_argv

    code = excinfo.value.code
    if code is None:
        code = 0
    captured = capsys.readouterr()
    return _ExecResult(code, captured.out, captured.err)


def _invoke(
    os_name: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
    *,
    target_body: Optional[str],
    argv: Optional[List[str]] = None,
    resolution_ok: bool = True,
) -> _ExecResult:
    argv = [] if argv is None else argv
    if os_name == "posix":
        return _invoke_posix_subprocess(tmp_path, argv, target_body=target_body, resolution_ok=resolution_ok)
    return _invoke_nt_inprocess(tmp_path, monkeypatch, capsys, argv, target_body=target_body, resolution_ok=resolution_ok)


def test_posix_forwarder_execs_no_shebang_no_exec_bit_target_via_real_subprocess(tmp_path):
    body = (
        "import sys\n"
        "def main(argv):\n"
        "    print('ran:' + ' '.join(argv))\n"
        "    return 5\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main(sys.argv[1:]))\n"
    )
    result = _invoke_posix_subprocess(
        tmp_path, ["a", "b c"], target_body=body, resolution_ok=True, target_mode=0o644,
    )

    fixture_path = tmp_path / "claude-klabauter-live-root" / "coordinator" / "bin" / _FIXTURE_TARGET_NAME
    mode = fixture_path.stat().st_mode
    assert not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)), "fixture must carry no exec bit"
    assert fixture_path.read_bytes()[:2] != b"#!", "fixture must carry no shebang"

    assert result.returncode == 5
    assert result.stdout.strip() == "ran:a b c"


@pytest.mark.skipif(
    os.name == "nt"
    or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="os.chmod(path, 0o000) cannot deny the owner read access on "
    "NTFS -- os.access(path, os.R_OK) still reports True for one's own "
    "file regardless of the mode bits passed (verified empirically), so "
    "there is no way to construct an actually-unreadable-to-self target "
    "on Windows for this falsifier to exercise. Same miss for a different "
    "reason as root (routine for a cloud container's own process): the "
    "kernel skips DAC permission checks for uid 0 entirely, so root's own "
    "open() of a 0o000 file still succeeds regardless of mode bits -- "
    "surfaced only once the COORDINATOR_ENGINE_ROOT env leak this module's "
    "other tests were fixed for (see _invoke_posix_subprocess) stopped "
    "masking every case here behind a uniform 127",
)
def test_posix_forwarder_execs_unreadable_target_via_real_subprocess(tmp_path):
    result = _invoke_posix_subprocess(
        tmp_path, [], target_body="print(1)\n", resolution_ok=True, target_mode=0o000,
    )

    fixture_path = tmp_path / "claude-klabauter-live-root" / "coordinator" / "bin" / _FIXTURE_TARGET_NAME
    mode = fixture_path.stat().st_mode
    assert not (mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)), "fixture must carry no read permission"

    assert result.returncode == 127
    assert "is missing" in result.stderr
    assert _REMEDIATION_TEXT in result.stderr


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_exit_code_fidelity_nonzero_propagates(os_name, tmp_path, monkeypatch, capsys):
    body = (
        "import sys\n"
        "def main(argv):\n"
        "    return 42\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main(sys.argv[1:]))\n"
    )
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body=body)
    assert result.returncode == 42


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_exit_code_fidelity_falls_off_end_returns_zero(os_name, tmp_path, monkeypatch, capsys):
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body="x = 1 + 1\n")
    assert result.returncode == 0


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_sys_exit_none_propagates_zero(os_name, tmp_path, monkeypatch, capsys):
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body="import sys\nsys.exit()\n")
    assert result.returncode == 0


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_sys_exit_string_message_normalizes_to_one(os_name, tmp_path, monkeypatch, capsys):
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body="import sys\nsys.exit('boom')\n")
    assert result.returncode == 1
    assert "boom" in result.stderr


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_argv_fidelity_and_restoration(os_name, tmp_path, monkeypatch, capsys):
    body = (
        "import sys\n"
        f"print('argv0-ok:' + str(sys.argv[0].endswith({_FIXTURE_TARGET_NAME!r})))\n"
        "print('argv:' + '|'.join(sys.argv[1:]))\n"
    )
    original_argv = list(sys.argv)
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body=body, argv=["a", "b c"])
    assert sys.argv == original_argv, "the caller's own sys.argv must be untouched after exec_cli returns/exits"
    assert "argv0-ok:True" in result.stdout
    assert "argv:a|b c" in result.stdout


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_missing_target_exits_127_with_remediation_message(os_name, tmp_path, monkeypatch, capsys):
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body=None)
    assert result.returncode == 127
    assert "is missing" in result.stderr
    assert _REMEDIATION_TEXT in result.stderr


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_resolution_failure_exits_1(os_name, tmp_path, monkeypatch, capsys):
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body="", resolution_ok=False)
    assert result.returncode == 1


def test_run_target_in_process_puts_claude_klabauter_root_on_sys_path(tmp_path):
    real_claude_klabauter_root = _MODULE_PATH.resolve().parents[3]
    assert (real_claude_klabauter_root / "coordinator_core" / "__init__.py").is_file(), (
        "sanity: this test relies on the real on-disk claude-klabauter root actually "
        "containing the coordinator_core package"
    )

    target = tmp_path / "target_imports_coordinator_core.py"
    target.write_text(
        "import coordinator_core\nprint('imported-ok')\n", encoding="utf-8"
    )

    driver = tmp_path / "driver.py"
    driver.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('_r', {str(_MODULE_PATH)!r})\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = m\n"
        "spec.loader.exec_module(m)\n"
        f"code = m._run_target_in_process({str(target)!r}, [], {str(real_claude_klabauter_root)!r})\n"
        "sys.exit(code)\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-S", str(driver)],
        cwd=str(tmp_path),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        **no_console_creationflags(),
    )

    assert result.returncode == 0, (
        f"target import failed under a process without the claude-klabauter root "
        f"ambient on sys.path -- stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "imported-ok" in result.stdout


def test_windows_branch_never_calls_os_execv(tmp_path, monkeypatch):
    _stub_bin_dir(monkeypatch, tmp_path / "coordinator" / "bin", "archive-stamp-cli", body="x = 1\n")
    proxy = _patch_os_name(monkeypatch, "nt")

    def _fail_execv(*_a, **_k):
        raise AssertionError("os.execv must not be called on the Windows branch")

    monkeypatch.setattr(proxy, "execv", _fail_execv)

    with pytest.raises(SystemExit) as excinfo:
        resolve_claude_klabauter.exec_cli("archive-stamp-cli", argv=[])

    assert excinfo.value.code == 0


def test_posix_branch_execs_via_sys_executable_with_target_path_and_argv(tmp_path, monkeypatch):
    target_path = _stub_bin_dir(monkeypatch, tmp_path / "coordinator" / "bin", "archive-stamp-cli")

    proxy = _patch_os_name(monkeypatch, "posix")

    captured: dict = {}

    def _fake_execv(path: str, argv: List[str]):
        captured["path"] = path
        captured["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(proxy, "execv", _fake_execv)

    with pytest.raises(SystemExit):
        resolve_claude_klabauter.exec_cli("archive-stamp-cli", argv=["--foo"])

    assert captured["path"] == sys.executable
    assert captured["argv"] == [sys.executable, target_path, "--foo"]


def test_posix_branch_execv_oserror_of_any_cause_still_exits_127(tmp_path, monkeypatch, capsys):
    _stub_bin_dir(monkeypatch, tmp_path / "coordinator" / "bin", "archive-stamp-cli")

    proxy = _patch_os_name(monkeypatch, "posix")

    def _fake_execv(path: str, argv: List[str]):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(proxy, "execv", _fake_execv)

    with pytest.raises(SystemExit) as excinfo:
        resolve_claude_klabauter.exec_cli("archive-stamp-cli")

    assert excinfo.value.code == 127
    err = capsys.readouterr().err
    assert "is missing or not executable" in err
    assert _REMEDIATION_TEXT in err


def test_run_target_in_process_puts_target_dir_on_sys_path(tmp_path):
    script_dir = tmp_path / "bin"
    sibling = script_dir / "_exec_cli_sibling_probe"
    sibling.mkdir(parents=True)
    (sibling / "__init__.py").write_text("VALUE = 'sibling-ok'\n", encoding="utf-8")

    target = script_dir / "target_imports_sibling.py"
    target.write_text(
        "from _exec_cli_sibling_probe import VALUE\nprint(VALUE)\n", encoding="utf-8"
    )

    assert str(script_dir) not in sys.path, (
        "sanity: the target's directory must not already be on sys.path, or "
        "this test cannot falsify a missing insert"
    )

    before = list(sys.path)
    code = resolve_claude_klabauter._run_target_in_process(
        str(target), [], str(_MODULE_PATH.resolve().parents[3])
    )

    assert code == 0
    assert sys.path == before, "sys.path must be restored, not merely popped"


def test_run_target_dir_insert_is_falsifiable(tmp_path):
    script_dir = tmp_path / "bin"
    sibling = script_dir / "_exec_cli_sibling_probe_neg"
    sibling.mkdir(parents=True)
    (sibling / "__init__.py").write_text("VALUE = 'x'\n", encoding="utf-8")

    target = script_dir / "target_imports_sibling_neg.py"
    target.write_text(
        "from _exec_cli_sibling_probe_neg import VALUE\n", encoding="utf-8"
    )

    import runpy

    with pytest.raises(ModuleNotFoundError):
        runpy.run_path(str(target), run_name="__main__")
