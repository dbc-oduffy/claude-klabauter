"""
Tests for coordinator_core.ops.coordinator_doe_root.

Mirrors the bash oracle's own test coverage (T1-T5) plus a rung-2.5-specific
case (T6) the bash suite exercises only indirectly via T3's fallthrough.
Scenarios are driven the same way the bash tests drive them: a fake
`machine-local` stub placed first on PATH, environment variables scoped per-test via
monkeypatch, and a fresh `os.environ["REPO_EXAMPLE_DOCTRINE_REPO"]` state.

Port of: coordinator-doe-root.test.sh (example-doctrine-repo 09e5e5f9, 2026-07-19)

NOTE (2026-07-21): the module is no longer process-global-state-bearing via
`os.environ` — the bash oracle's `export` was retired because it leaked across the
shared pytest interpreter (see the module's own docstring § DECISION REVERSAL). The
same-process re-resolution guard is now an explicit module-scope memo, which
`_clean_env` below resets per-test. T2/T5/T6 assert the ABSENCE of the export.
"""

from __future__ import annotations

import os
import stat
import textwrap

import pytest

from coordinator_core.ops import coordinator_doe_root as mod
from coordinator_core.testing.fake_machine_local import write_fake_machine_local


def _write_stub(path, python_body: str) -> str:
    """Write a fake `machine-local` CLI at `path`, resolved via `shutil.which` in
    `coordinator_doe_root.py` -- see `coordinator_core.testing.fake_machine_local`
    for the Windows PATHEXT/exec rationale. `python_body` is Python source (reads
    `sys.argv[1:]`), not a shell script -- callers below were ported from bash
    stub bodies to Python bodies for cross-platform execution."""
    return str(write_fake_machine_local(path, python_body))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Reset the module-scope resolution memo before AND after each test: it is
    # interpreter-lifetime state (correct under spawn-per-call, shared under pytest),
    # so a value pinned by one test would otherwise leak into the next. Mirrors the
    # _reset_central_root_memo fixture in test_deliverable_rollup.py.
    mod._reset_doe_root_cache()
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    yield
    mod._reset_doe_root_cache()


def test_t1_env_short_circuit_no_machine_local_call(tmp_path, monkeypatch):
    stubdir = tmp_path / "t1-stub"
    stubdir.mkdir()
    sentinel = tmp_path / "t1-called"
    _write_stub(
        str(stubdir),
        "import pathlib\n"
        f"pathlib.Path({str(sentinel)!r}).touch()\n"
        "print('/should-not-be-returned')\n",
    )
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", "/tmp/fake-doe-root")
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == "/tmp/fake-doe-root"
    assert not sentinel.exists()


def test_t2_registry_resolution(tmp_path, monkeypatch):
    stubdir = tmp_path / "t2-stub"
    stubdir.mkdir()
    expected = "/x/example-doctrine-repo"
    _write_stub(str(stubdir), f"print({expected!r})\n")
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == expected
    # Inverted 2026-07-21: the resolver is pure and no longer exports on rung 2.
    assert "REPO_EXAMPLE_DOCTRINE_REPO" not in os.environ


def test_t3_fail_loud_returns_none_and_remediation(tmp_path, monkeypatch, capsys):
    stubdir = tmp_path / "t3-stub"
    stubdir.mkdir()
    _write_stub(str(stubdir), "import sys\nsys.exit(1)\n")
    fake_home = tmp_path / "t3-empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()
    assert result is None

    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "repos.example_doctrine_repo" in captured.err


def test_t4_memo_idempotency_second_call_skips_machine_local(tmp_path, monkeypatch):
    """Renamed from `test_t4_export_idempotency_...` (2026-07-21): the
    single-machine-local-call property is now carried by the module-scope memo
    rather than by the retired `os.environ["REPO_EXAMPLE_DOCTRINE_REPO"]` export. The
    assertion is unchanged -- only the mechanism under it moved."""
    stubdir = tmp_path / "t4-stub"
    stubdir.mkdir()
    sentinel = tmp_path / "t4-call-count"
    _write_stub(
        str(stubdir),
        "with open(" + repr(str(sentinel)) + ", 'a') as _f:\n"
        "    _f.write('called\\n')\n"
        "print('/x/example-doctrine-repo')\n",
    )
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    first = mod.coordinator_doe_root()
    second = mod.coordinator_doe_root()

    assert first == "/x/example-doctrine-repo"
    assert second == "/x/example-doctrine-repo"
    assert sentinel.read_text().count("called\n") == 1


def test_t5_rung3_pointer_file_fallback_via_clone_root_script(tmp_path, monkeypatch):
    """C11 (2026-07-21): rung 3 (`_resolve_via_clone_root_script`) now calls the
    native `coordinator_core.resolve_coordinator_clone.resolve_clone_root()` port
    in-process instead of shelling to a fake `resolve-coordinator-clone.sh`. The
    on-disk fake script below is left in place but is NOT invoked -- the native
    resolver's own `.doe-root`-pointer-plus-`.git` rung resolves directly to
    `fake_doe_root`, exercising the same rung-3 fallback path this test was
    originally written to characterize, just through the ported module rather
    than a subprocess.
    """
    stubdir = tmp_path / "t5-stub"
    stubdir.mkdir()
    _write_stub(str(stubdir), "import sys\nsys.exit(1)\n")

    fake_home = tmp_path / "t5-fake-home"
    fake_doe_root = tmp_path / "t5-fake-doe-root"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_doe_root / "coordinator" / "lib").mkdir(parents=True)
    (fake_doe_root / ".git").mkdir()
    (fake_home / ".claude" / ".doe-root").write_text(str(fake_doe_root))

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == str(fake_doe_root)
    # The resolver is PURE as of 2026-07-21 -- it no longer exports REPO_EXAMPLE_DOCTRINE_REPO
    # (see the module docstring's DECISION REVERSAL section). This assertion was
    # inverted from `os.environ["REPO_EXAMPLE_DOCTRINE_REPO"] == str(fake_doe_root)`: it now
    # pins the absence of the interpreter-global write, which is the property that
    # actually matters to every other test in the suite.
    assert "REPO_EXAMPLE_DOCTRINE_REPO" not in os.environ


def test_t6_rung25_live_path_fallback(tmp_path, monkeypatch):
    """Rung 2 (repos.example_doctrine_repo) empty, rung 2.5 (live_path) resolves."""
    stubdir = tmp_path / "t6-stub"
    stubdir.mkdir()
    _write_stub(
        str(stubdir),
        textwrap.dedent(
            """\
            import sys
            argv2 = sys.argv[2] if len(sys.argv) > 2 else None
            if argv2 == "repos.example_doctrine_repo":
                sys.exit(1)
            if argv2 == "plugin.mirrors.coordinator-claude.live_path":
                print("/x/live-path-doe")
                sys.exit(0)
            sys.exit(1)
            """
        ),
    )
    fake_home = tmp_path / "t6-empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == "/x/live-path-doe"
    # Inverted 2026-07-21: the resolver is pure and no longer exports on rung 2.5.
    assert "REPO_EXAMPLE_DOCTRINE_REPO" not in os.environ


def test_negative_no_machine_local_no_pointer_file(tmp_path, monkeypatch):
    """Negative corpus: machine-local absent from PATH entirely, no pointer file,
    no COORDINATOR_ROOT/CLAUDE_PLUGIN_ROOT override -- must fail loud (None), never
    raise."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "neg-empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result is None

    rc = mod.main([])
    assert rc == 1


def test_main_cli_success_prints_no_trailing_newline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", "/tmp/some-doe-root")
    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "/tmp/some-doe-root"
