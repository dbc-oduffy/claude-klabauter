"""Tests for coordinator_core.install.door_install_posix_build: build when a
POSIX C toolchain is present, degrade to a runnable (never slash-command)
advisory when it isn't -- never raising on a toolchain miss.

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C4.md
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys

from pathlib import Path

import pytest

from coordinator_core.install import door_install_posix_build as posix_install
from coordinator_core.warm.door import build_posix
from coordinator_core.win_portability import no_console_creationflags


#: The real engine root, derived from this file rather than from cwd so the
#: execution leg below works under any invocation directory.
#: tests/ -> install/ -> coordinator_core/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="door_install_posix_build is POSIX-only"
)


def _stamp_engine_root(root):
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def test_has_posix_compiler_matches_find_compiler(monkeypatch):
    # `has_posix_compiler` must agree with `build_posix._find_compiler` --
    # it is a non-raising wrapper around that same detector, not a second
    # probe with its own candidate list.
    try:
        build_posix._find_compiler(None)
        expect_hit = True
    except SystemExit:
        expect_hit = False
    assert posix_install.has_posix_compiler() is expect_hit


def test_has_posix_compiler_false_when_find_compiler_raises(monkeypatch):
    def _raise(requested):
        raise SystemExit("no compiler found")

    monkeypatch.setattr(build_posix, "_find_compiler", _raise)
    assert posix_install.has_posix_compiler() is False


def test_build_or_advise_returns_advisory_on_toolchain_miss(tmp_path, monkeypatch):
    def _raise(requested):
        raise SystemExit("no compiler found")

    monkeypatch.setattr(build_posix, "_find_compiler", _raise)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    result = posix_install.build_or_advise(engine_root)

    assert result.built is False
    assert result.output is None
    assert result.advisory is not None
    assert "python3 " in result.advisory
    assert "coordinator:" not in result.advisory


def test_build_or_advise_advisory_command_actually_runs(tmp_path, monkeypatch):
    """The advisory's named command must EXECUTE, not merely mention the
    builder.

    This test replaces one that asserted `"build_posix.py" in result.advisory`.
    That assertion held while the advisory emitted
    `python3 <abs path>/build_posix.py <root>` -- a command that dies on
    `ImportError: attempted relative import with no known parent package`
    before reaching its own argparse, because `build_posix.py` does
    `from .build import write_sidecar` and a file-path invocation gives it no
    package context. A cold-path remediation that cannot run is exactly what
    the runnable-remediation rule exists to prevent, and a guard that checks
    the spelling instead of the behaviour cannot fire on it. So: extract the
    command from the advisory and run it.
    """
    def _raise(requested):
        raise SystemExit("no compiler found")

    monkeypatch.setattr(build_posix, "_find_compiler", _raise)

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    result = posix_install.build_or_advise(engine_root)

    assert str(engine_root.resolve()) in result.advisory

    marker = "build it later with: "
    assert marker in result.advisory
    command = result.advisory.split(marker, 1)[1].strip()
    tokens = command.split()

    # Leading `KEY=VALUE` tokens are shell env assignments, not argv. The
    # advisory carries `PYTHONPATH=<engine root>` because the module route
    # alone is not enough: a bare `python3 -m coordinator_core.warm.door.
    # build_posix` dies on `ModuleNotFoundError: No module named
    # 'coordinator_core'` unless the engine root is importable, and an operator
    # reading this advisory is by definition not sitting inside the engine tree.
    #
    # Honouring the prefix here rather than asserting it away is the point. This
    # test's whole job is "the command AS WRITTEN runs", and the previous
    # version modelled a command as bare argv -- so it asserted `argv[0] ==
    # "python3"`, could not express an env prefix, and went red the moment the
    # advisory was fixed to actually work. A test that cannot represent the
    # correct answer will keep reporting the fix as the failure.
    env_prefix: "dict[str, str]" = {}
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        key, _, value = tokens[0].partition("=")
        env_prefix[key] = value
        tokens = tokens[1:]

    argv = tokens
    assert argv[0] == "python3"
    assert env_prefix.get("PYTHONPATH") == str(engine_root.resolve()), (
        "the advisory must make the engine root importable, or the module route "
        f"it names cannot resolve: {command}"
    )

    # The execution check below proves "it runs"; these two prove "it is the
    # module route". Both are needed: the defect that shipped green was a
    # `.py` file path in the advisory, and a future edit could reintroduce one
    # wrapped in something that still exits 0 on --help. The old test asserted
    # only the spelling and the new one would have asserted only the running.
    assert ".py" not in command, f"advisory must not name a file path: {command}"
    assert argv[1:3] == ["-m", "coordinator_core.warm.door.build_posix"], argv
    argv[0] = sys.executable

    # `--help` exercises import + argparse construction, which is where the
    # file-path spelling failed, without paying a real compile.
    completed = subprocess.run(
        [*argv[:-1], "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
        # Run the command's own SHAPE -- `PYTHONPATH=<root> python3 -m ...` --
        # rather than a bare argv, because running it without the prefix is what
        # the operator never does and what let this check pass on a command that
        # could not work.
        #
        # The PYTHONPATH value is swapped to the real engine root for the
        # execution leg only. `engine_root` above is a stamped tmp_path skeleton
        # with no `coordinator_core/warm/` in it, so the advisory's literal value
        # is right for the assertion (it must name the root it was given) and
        # useless for actually importing the module. The two legs check
        # different things: the assertion above proves the advisory names the
        # right root, this proves the command FORM resolves when the root is
        # genuine. `cwd=tmp_path` keeps the repo from being importable by
        # accident, so PYTHONPATH is the only thing that can resolve it.
        env={
            **os.environ,
            **env_prefix,
            "PYTHONPATH": str(_REPO_ROOT),
        },
        **no_console_creationflags(),
    )
    assert completed.returncode == 0, (
        "the advisory names a command that does not run:\n"
        f"  command: {command}\n"
        f"  stderr:  {completed.stderr}"
    )
    assert "engine_root" in completed.stdout


def test_build_or_advise_builds_when_toolchain_present(tmp_path):
    if not posix_install.has_posix_compiler():
        pytest.skip("no C compiler on PATH in this environment")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    output = tmp_path / "door-out"

    result = posix_install.build_or_advise(engine_root, output=output)

    assert result.built is True
    assert result.advisory is None
    assert result.output == output
    assert output.exists()


def test_build_or_advise_output_is_executable_under_restrictive_umask(tmp_path):
    # `clang -O2 -o out t.c` under `umask 0177` writes `-rw-------` -- zero
    # exec bits at all, verified directly (2026-08-22). A restrictive umask
    # is ambient install-time state this best-effort/advisory POSIX path
    # must not depend on, so `build_or_advise` chmods the output itself
    # rather than trusting whatever the compiler happened to write.
    if not posix_install.has_posix_compiler():
        pytest.skip("no C compiler on PATH in this environment")

    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    output = tmp_path / "door-out"

    old_umask = os.umask(0o177)
    try:
        result = posix_install.build_or_advise(engine_root, output=output)
    finally:
        os.umask(old_umask)

    assert result.built is True
    mode = stat.S_IMODE(output.stat().st_mode)
    assert mode & stat.S_IXUSR, f"expected owner-exec bit set, got {oct(mode)}"


def test_build_or_advise_refuses_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)

    with pytest.raises(SystemExit):
        posix_install.build_or_advise(engine_root)
