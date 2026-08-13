"""
coordinator_core.install.test_resolve_claude_klabauter_exec_cli — unit coverage of
``exec_cli``'s Windows/POSIX platform branch in
``coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py``.

Companion to ``test_resolve_claude_klabauter.py`` (which covers ``resolve_claude_klabauter_bin_dir``'s
reject-path ladder directly). This file covers the platform dispatch inside
``exec_cli`` itself.

POSIX leg (post-convergence, 2026-07-31): ``exec_cli`` now execs
``os.execv(sys.executable, [sys.executable, target_path, *argv])`` — the
current *interpreter*, not the bare target — so a ``coordinator/bin`` CLI
with no ``#!`` shebang and no exec bit is invocable exactly like one that
has both. Every existing test in this module used to monkeypatch
``resolve_claude_klabauter_bin_dir`` and run the target INSIDE the pytest process —
that convention would pass identically whether or not the sys.executable
retarget landed (same in-process sys.path/argv either way), so it cannot
serve as this convergence's falsifier. The load-bearing POSIX coverage
below (``test_posix_forwarder_execs_no_shebang_no_exec_bit_target_via_real_subprocess``)
therefore generates a REAL forwarder into a tmp settings-home and invokes it
as a genuinely separate process via ``subprocess.run`` — see that test's
docstring. Everything else in the POSIX section that only probes the
``os.execv`` call shape stays a monkeypatch, matching precedent.

Windows leg (unchanged): ``os.execv`` cannot honor a POSIX shebang
(``CreateProcess`` does not interpret ``#!``) and cannot truly replace the
current process image, so ``exec_cli`` runs the target **in-process** via
``runpy.run_path`` (``_run_target_in_process``) instead. There is no real
Windows host to subprocess against here, so this leg is exercised by
monkeypatching ``os.name`` (never actually invoking a real Windows process),
while ``os.execv`` itself is monkeypatched to fail the test if the Windows
branch ever calls it.

Behavior genuinely shared between the two legs (exit-code fidelity, argv
fidelity and restoration, missing-target -> 127, resolution-failure -> 1) is
covered ONCE via a single parametrized ``os_name in {"posix", "nt"}`` suite
that dispatches to a real subprocess (POSIX) or an in-process monkeypatch
(NT) behind a common ``_invoke`` helper returning a normalized
(exit-code, stdout, stderr) shape — so a POSIX-only regression cannot go
green on Windows CI merely because it shares a test id with the Windows
case. Only genuinely mechanism-specific pre-flight checks (e.g. "Windows
never calls os.execv"; "POSIX composes execv's argv as
[sys.executable, target_path, *argv]") stay outside that shared suite.

Module-loading convention (importlib.util.spec_from_file_location) matches
``test_resolve_claude_klabauter.py`` — the module under test is installed standalone
into a bare bin/ directory, deliberately import-independent of
coordinator_core, and lives under a hyphenated directory name that
precludes a normal ``import``.

Spec backlink: docs/plans/2026-07-24-canonical-resolution-engine.md W0-3
(AC-8) — replaces the prior subprocess-of-a-second-interpreter Windows
branch this file used to cover.
Spec backlink: pln-converge-exec-cli-s-posix-leg--d7e29a
C3 (AC2, AC3, AC4) — the real-subprocess POSIX falsifier and the shared
parametrized suite below.
"""
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

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
)

_spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_under_test_exec_cli", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
resolve_claude_klabauter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = resolve_claude_klabauter
_spec.loader.exec_module(resolve_claude_klabauter)

_FIXTURE_TARGET_NAME = "fixture-cli"


class _OSNameProxy:
    """Substitutes for ``resolve_claude_klabauter``'s module-level ``os`` name so a
    test can flip the ``os.name`` branch ``exec_cli``/``_is_executable``
    read without mutating the real, process-global ``os`` module.

    ``resolve_claude_klabauter.os`` IS the real ``os`` module object (a plain
    ``import os``, not a copy) — the module docstring for this test file
    calls out that ``monkeypatch.setattr(resolve_claude_klabauter.os, "name", ...)``
    corrupts ``pathlib``'s platform dispatch for the rest of the test
    process, surfacing as ``PosixPath cannot instantiate on your system``
    on a later, unrelated test. Patching the NAME ``resolve_claude_klabauter.os``
    itself (via ``monkeypatch.setattr(resolve_claude_klabauter, "os", proxy)``)
    instead of an attribute on the shared module object gives ``exec_cli``
    a `.name` it reads directly while every other attribute access
    (``os.path``, ``os.stat``, ``os.environ``, ``os.execv`` once a test
    monkeypatches that too, ...) transparently forwards to the real
    module — restored automatically by ``monkeypatch``'s teardown, and
    never touching the real ``os`` module at all.
    """

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "name", name)

    def __getattr__(self, attr):
        return getattr(os, attr)


def _patch_os_name(monkeypatch, name: str) -> "_OSNameProxy":
    proxy = _OSNameProxy(name)
    monkeypatch.setattr(resolve_claude_klabauter, "os", proxy)
    return proxy


def _patch_root_resolution(monkeypatch, bin_dir: Path) -> None:
    """Point ``exec_cli``'s post-C4b root resolution at *bin_dir* directly,
    bypassing ``resolve_claude_klabauter_root_with_class()``'s registry-then-sentinel
    ladder and ``_validate_bin_dir()``'s on-disk sentinel probe.

    Mirrors the pre-C4b convention (monkeypatching ``resolve_claude_klabauter_bin_dir``
    wholesale) for the two calls ``exec_cli`` makes today:
    ``resolve_claude_klabauter_root_with_class()`` (root + resolution class) then
    ``_validate_bin_dir(root)`` (dir + sentinel validation). Patching only
    ``resolve_claude_klabauter_bin_dir`` (as the pre-C4b tests did) intercepts
    nothing post-C4b — ``exec_cli`` no longer calls it on the primary path
    — which is exactly why these 9 tests silently fell through to this
    operator's real, unconfigured settings home and blew up on
    ``ClaudeKlabauterResolutionError``.
    """
    monkeypatch.setattr(
        resolve_claude_klabauter,
        "resolve_claude_klabauter_root_with_class",
        lambda: (str(bin_dir), resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE),
    )
    monkeypatch.setattr(resolve_claude_klabauter, "_validate_bin_dir", lambda root: root)
    monkeypatch.setattr(resolve_claude_klabauter, "resolve_claude_klabauter_bin_dir", lambda: str(bin_dir))


def _stub_bin_dir(monkeypatch, bin_dir: Path, target_name: str, *, body: str = "", create_target: bool = True) -> str:
    """Point exec_cli's root resolution at *bin_dir* and optionally create an
    on-disk *target_name* file inside it (with *body*, defaulting to a bare
    shebang line), returning the expected ``target_path`` string
    ``exec_cli`` will compose."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if create_target:
        (bin_dir / target_name).write_text(body or "#!/usr/bin/env python3\n", encoding="utf-8")
    _patch_root_resolution(monkeypatch, bin_dir)
    return str(bin_dir) + "/" + target_name


# ---------------------------------------------------------------------------
# Real-subprocess POSIX harness — a real generated forwarder in a tmp
# settings-home, exec'd out-of-process, never the pytest process itself.
# ---------------------------------------------------------------------------


def _write_sentinel(coord_bin: Path) -> None:
    """Write the executable ``archive-stamp-cli`` sentinel ``resolve_claude_klabauter_bin_dir``
    probes before returning ``coord_bin`` as a valid ``coordinator/bin``."""
    sentinel = coord_bin / "archive-stamp-cli"
    sentinel.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    sentinel.chmod(0o755)
    if os.name == "nt":
        # `_resolve_claude_klabauter.py`'s Windows-side executability probe is
        # PATHEXT-based, not stat-mode-based (NTFS has no exec bit for
        # os.chmod to set) — mirror the real on-disk archive-stamp-cli's
        # `.cmd` companion, matching test_forwarder_trust_guard.py's fixture.
        (coord_bin / "archive-stamp-cli.cmd").write_text("@echo SENTINEL\r\n", encoding="utf-8")


def _write_forwarder(bin_dir: Path, forwarder_name: str, target: str) -> Path:
    """Write a real generated-forwarder-shaped file into *bin_dir*, matching
    ``substrate._write_agent_forwarder``'s installed shape: import the
    co-located ``_resolve_claude_klabauter.py`` and call ``exec_cli(target)``."""
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
        claude_klabauter_root = tmp_path / "claude-klabauter-root"
        coord_bin = claude_klabauter_root / "coordinator" / "bin"
        coord_bin.mkdir(parents=True)
        _write_sentinel(coord_bin)
        if target_body is not None:
            target_path = coord_bin / _FIXTURE_TARGET_NAME
            target_path.write_text(target_body, encoding="utf-8")
            target_path.chmod(target_mode)
        # TOML literal string (single quotes) — claude_klabauter_root is a raw
        # filesystem path and on Windows carries backslashes (e.g.
        # `C:\Users\...`); a TOML basic string (double quotes) interprets
        # backslash escape sequences, so `\U...` etc. would raise a TOML
        # parse error there. A literal string performs no escape
        # processing at all — matches the convention already used by the
        # companion test_resolve_claude_klabauter.py.
        (ml_dir / "registry.local.toml").write_text(
            f"[repos]\nclaude_klabauter = '{claude_klabauter_root}'\n", encoding="utf-8"
        )
    # else: leave machine-local/ empty -> _resolve_claude_klabauter_root raises
    # ClaudeKlabauterResolutionError inside the forwarder's own process.

    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = str(settings_home)

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
    """NT leg never calls ``os.execv`` (runs the target in-process via
    ``runpy``), so there is no real second process to subprocess against
    even in principle — stays an in-process ``os.name`` monkeypatch."""
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
    """Dispatch to the real-subprocess POSIX harness or the in-process NT
    harness, returning a normalized (returncode, stdout, stderr) shape so
    the shared-behavior tests below can assert identically over both."""
    argv = [] if argv is None else argv
    if os_name == "posix":
        return _invoke_posix_subprocess(tmp_path, argv, target_body=target_body, resolution_ok=resolution_ok)
    return _invoke_nt_inprocess(tmp_path, monkeypatch, capsys, argv, target_body=target_body, resolution_ok=resolution_ok)


# ---------------------------------------------------------------------------
# AC2's falsifier -- real subprocess, no shebang, no exec bit
# ---------------------------------------------------------------------------


def test_posix_forwarder_execs_no_shebang_no_exec_bit_target_via_real_subprocess(tmp_path):
    """AC2's falsifier: a real generated forwarder, invoked as a genuinely
    separate process, must run a fixture CLI carrying NEITHER a `#!`
    shebang NOR an exec bit (mode 0644) — the whole point of retargeting
    ``os.execv`` at ``sys.executable`` instead of the bare target path. An
    in-process monkeypatch of ``resolve_claude_klabauter_bin_dir`` would pass
    identically before and after that retarget (see module docstring) and
    is deliberately NOT used here."""
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

    fixture_path = tmp_path / "claude-klabauter-root" / "coordinator" / "bin" / _FIXTURE_TARGET_NAME
    mode = fixture_path.stat().st_mode
    assert not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)), "fixture must carry no exec bit"
    assert fixture_path.read_bytes()[:2] != b"#!", "fixture must carry no shebang"

    assert result.returncode == 5
    assert result.stdout.strip() == "ran:a b c"


@pytest.mark.skipif(
    os.name == "nt",
    reason="os.chmod(path, 0o000) cannot deny the owner read access on "
    "NTFS -- os.access(path, os.R_OK) still reports True for one's own "
    "file regardless of the mode bits passed (verified empirically), so "
    "there is no way to construct an actually-unreadable-to-self target "
    "on Windows for this falsifier to exercise",
)
def test_posix_forwarder_execs_unreadable_target_via_real_subprocess(tmp_path):
    """Review: code-reviewer F1's falsifier — a real generated forwarder,
    invoked as a genuinely separate process, against a fixture CLI that
    exists but carries no read permission (mode 0o000). Before the
    isfile()+os.access(R_OK) pre-check, `os.execv(sys.executable, [...])`
    never raises for this case (sys.executable itself always exists and is
    executable) — the failure instead surfaced *after* process replacement,
    inside the second interpreter's own `open()` of target_path, as CPython's
    own "can't open file" message and exit code 2, losing the contracted 127
    + remediation text. Empirically confirmed pre-fix (exit 2, CPython
    message) and post-fix (exit 127, remediation message) by hand before
    this test was written; see the P1 finding this closes."""
    result = _invoke_posix_subprocess(
        tmp_path, [], target_body="print(1)\n", resolution_ok=True, target_mode=0o000,
    )

    fixture_path = tmp_path / "claude-klabauter-root" / "coordinator" / "bin" / _FIXTURE_TARGET_NAME
    mode = fixture_path.stat().st_mode
    assert not (mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)), "fixture must carry no read permission"

    assert result.returncode == 127
    assert "is missing" in result.stderr
    assert "re-run coordinator:install" in result.stderr


# ---------------------------------------------------------------------------
# Shared behavior -- parametrized over os_name, single assertion set, so a
# POSIX-only regression cannot go green on Windows CI under a shared id.
# ---------------------------------------------------------------------------


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
    assert "re-run coordinator:install" in result.stderr


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_resolution_failure_exits_1(os_name, tmp_path, monkeypatch, capsys):
    result = _invoke(os_name, tmp_path, monkeypatch, capsys, target_body="", resolution_ok=False)
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# sys.path regression -- `_run_target_in_process` must put the claude-klabauter root
# on sys.path for the forwarded target, or any target that imports
# `coordinator_core` at module top dies with ModuleNotFoundError before
# running a line of its own logic (2026-08-07 cross-repo memo:
# cross-repo/inbox/2026-08-07-coordinator-claude-em-settings-home-forwarder-drops-
# coordinator-core-from-syspath.md).
#
# The prior coverage in this module could not catch this class: every other
# NT-leg test above runs the target INSIDE the pytest process, where
# `coordinator_core` is already importable regardless of what
# `_run_target_in_process` does to `sys.path` — a fixture that merely added
# the import would go green whether or not the insert existed. Worse, on
# THIS box `coordinator_core` is pip-installed in editable mode
# (`site-packages/__editable__.coordinator_core-0.0.0.pth`), so even a naive
# real-subprocess test would pass regardless of the fix, because the
# interpreter's own `site.py` machinery makes the package importable
# ambient to every subprocess launched with this interpreter. `-S` (skip
# `site.py`/`.pth` processing) is the one flag that genuinely defeats that
# ambient importability, producing a process where `coordinator_core` is
# NOT on `sys.path` unless something inserts it — verified empirically:
# `python -S` against this driver raises `ModuleNotFoundError` pre-fix and
# succeeds post-fix. Single subprocess spawn, no loop.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Windows-specific pre-flight -- the in-process runpy mechanism itself,
# not expressible as an externally-observable outcome the POSIX leg shares.
# ---------------------------------------------------------------------------


def test_windows_branch_never_calls_os_execv(tmp_path, monkeypatch):
    _stub_bin_dir(monkeypatch, tmp_path / "coordinator" / "bin", "archive-stamp-cli", body="x = 1\n")
    proxy = _patch_os_name(monkeypatch, "nt")

    def _fail_execv(*_a, **_k):
        raise AssertionError("os.execv must not be called on the Windows branch")

    monkeypatch.setattr(proxy, "execv", _fail_execv)

    with pytest.raises(SystemExit) as excinfo:
        resolve_claude_klabauter.exec_cli("archive-stamp-cli", argv=[])

    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# POSIX-specific pre-flight -- the os.execv call-shape itself, not
# expressible as an externally-observable outcome the Windows leg shares.
# ---------------------------------------------------------------------------


def test_posix_branch_execs_via_sys_executable_with_target_path_and_argv(tmp_path, monkeypatch):
    target_path = _stub_bin_dir(monkeypatch, tmp_path / "coordinator" / "bin", "archive-stamp-cli")

    proxy = _patch_os_name(monkeypatch, "posix")

    captured: dict = {}

    def _fake_execv(path: str, argv: List[str]):
        captured["path"] = path
        captured["argv"] = argv
        # os.execv never returns on success; simulate that contract without
        # actually replacing the test process image.
        raise SystemExit(0)

    monkeypatch.setattr(proxy, "execv", _fake_execv)

    with pytest.raises(SystemExit):
        resolve_claude_klabauter.exec_cli("archive-stamp-cli", argv=["--foo"])

    assert captured["path"] == sys.executable
    assert captured["argv"] == [sys.executable, target_path, "--foo"]


def test_posix_branch_execv_oserror_of_any_cause_still_exits_127(tmp_path, monkeypatch, capsys):
    """Not a real-input falsifier (Review: code-reviewer F2) — the isfile()
    + os.access(R_OK) pre-check now catches missing/unreadable targets
    before `os.execv` is ever reached, so a real "missing or not
    executable" target can no longer drive `os.execv` itself to raise. This
    only proves the `except OSError` handler still does the right thing if
    `os.execv` raises for ANY reason (e.g. `sys.executable` vanishing
    mid-run) — see
    ``test_posix_forwarder_execs_unreadable_target_via_real_subprocess``
    below for the genuine unreadable-target falsifier."""
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
    assert "re-run coordinator:install" in err
