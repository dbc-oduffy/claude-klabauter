"""Unit tests for coordinator_core.pyresolve -- specifically the
``prefer_windowless`` parameter added to ``resolve_python_bin()`` so a
general-purpose caller (e.g. install-substrate's ``python3.cmd`` bake) can
request the console interpreter instead of inheriting the windowless
preference meant for controlled-stdin hook spawns.

Regression context: ``python3.cmd`` baked ``pythonw.exe`` (a
``/SUBSYSTEM:WINDOWS`` binary with null std handles unless the child
explicitly redirects) as its interpreter. A general-purpose shim cannot
assume its caller controls stdin -- a real box hit a permanent, silent hang
where a loud fast failure was expected. See
``coordinator_core/install/substrate.py``'s ``_resolve_baked_python_bin()``
docstring for the full narrative and its own defense-in-depth coverage.
"""
from __future__ import annotations

import os

import pytest

from coordinator_core import pyresolve


@pytest.fixture(autouse=True)
def _clear_pyresolve_cache():
    """Every case gets a clean memoization cache -- a stale entry from a prior
    case's monkeypatched env/subprocess result would otherwise leak forward in
    file order (module-level dicts persist across tests within one process)."""
    pyresolve.clear_resolution_cache()
    yield
    pyresolve.clear_resolution_cache()


# --- prefer_windowless threading through the Windows OS-detect tier --------


def test_default_prefers_windowless_pyorg_probe_order(monkeypatch):
    """Regression pin: existing callers (e.g. plugin_health/sentinel.py's
    doctor-probe spawns) rely on the windowless-first console-flash
    suppression. The default must stay ``True`` so calling
    ``resolve_python_bin()`` with no argument reproduces today's behavior."""
    monkeypatch.setattr(pyresolve, "_is_windows", lambda: True)

    captured = {}

    def _fake_pyorg_search(prefer_windowless):
        captured["prefer_windowless"] = prefer_windowless
        return r"C:\Program Files\Python313\pythonw.exe" if prefer_windowless else None

    monkeypatch.setattr(pyresolve, "_pyorg_search", _fake_pyorg_search)
    monkeypatch.delenv("COORDINATOR_PYTHON", raising=False)
    monkeypatch.setattr(pyresolve, "_machine_local_get", lambda key: None)

    py_bin, _py_args = pyresolve.resolve_python_bin()
    assert captured["prefer_windowless"] is True
    assert py_bin == r"C:\Program Files\Python313\pythonw.exe"


def test_prefer_windowless_false_requests_console_probe_order(monkeypatch):
    monkeypatch.setattr(pyresolve, "_is_windows", lambda: True)

    captured = {}

    def _fake_pyorg_search(prefer_windowless):
        captured["prefer_windowless"] = prefer_windowless
        return r"C:\Program Files\Python313\python.exe" if not prefer_windowless else None

    monkeypatch.setattr(pyresolve, "_pyorg_search", _fake_pyorg_search)
    monkeypatch.delenv("COORDINATOR_PYTHON", raising=False)
    monkeypatch.setattr(pyresolve, "_machine_local_get", lambda key: None)

    py_bin, _py_args = pyresolve.resolve_python_bin(prefer_windowless=False)
    assert captured["prefer_windowless"] is False
    assert py_bin == r"C:\Program Files\Python313\python.exe"


def test_resolve_machine_python_bin_ignores_a_set_pin(monkeypatch):
    """``resolve_machine_python_bin`` must skip BOTH pin tiers even when a
    pin is present and would otherwise win under ``resolve_python_bin``'s
    own precedence -- the entire point of the function (docs/plans/
    2026-08-14-the-venv-fallback-stops-being-something.md C2)."""
    monkeypatch.setenv("COORDINATOR_PYTHON", "/pinned/shared/venv/python3")
    monkeypatch.setattr(pyresolve, "_machine_local_get", lambda key: "/pinned/ml/python3")
    monkeypatch.setattr(pyresolve, "_validate_interpreter", lambda path: True)
    monkeypatch.setattr(pyresolve, "_is_windows", lambda: False)
    monkeypatch.setattr(pyresolve, "_resolve_non_windows", lambda: ("/machine/python3", []))

    py_bin, py_args = pyresolve.resolve_machine_python_bin()

    assert py_bin == "/machine/python3"
    assert py_args == []


def test_prefer_windowless_false_reorders_path_fallback_probe_names(monkeypatch):
    """The PATH-fallback tier (Step 2 of ``_resolve_windows``) previously
    hardcoded ``pythonw`` first regardless of caller intent -- assert the
    probe order itself flips, not just the pyorg-search tier."""
    monkeypatch.setattr(pyresolve, "_is_windows", lambda: True)
    monkeypatch.setattr(pyresolve, "_pyorg_search", lambda prefer_windowless: None)
    monkeypatch.delenv("COORDINATOR_PYTHON", raising=False)
    monkeypatch.setattr(pyresolve, "_machine_local_get", lambda key: None)

    probed_names = []

    def _fake_which(name):
        probed_names.append(name)
        if name == "python3":
            return r"C:\Program Files\Python313\python3.exe"
        return None

    monkeypatch.setattr(pyresolve, "_which", _fake_which)

    py_bin, _py_args = pyresolve.resolve_python_bin(prefer_windowless=False)
    assert py_bin == r"C:\Program Files\Python313\python3.exe"
    assert probed_names[0] == "python3", (
        "prefer_windowless=False must probe a console-shaped name first"
    )


def test_prefer_windowless_true_keeps_pythonw_first_in_path_fallback(monkeypatch):
    monkeypatch.setattr(pyresolve, "_is_windows", lambda: True)
    monkeypatch.setattr(pyresolve, "_pyorg_search", lambda prefer_windowless: None)
    monkeypatch.delenv("COORDINATOR_PYTHON", raising=False)
    monkeypatch.setattr(pyresolve, "_machine_local_get", lambda key: None)

    probed_names = []

    def _fake_which(name):
        probed_names.append(name)
        return None

    monkeypatch.setattr(pyresolve, "_which", _fake_which)

    pyresolve.resolve_python_bin()
    assert probed_names[0] == "pythonw"


# --- memoization: _validate_interpreter and _machine_local_get -------------


def test_validate_interpreter_spawns_once_across_repeated_calls(monkeypatch):
    calls = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(pyresolve.subprocess, "run", _fake_run)

    assert pyresolve._validate_interpreter("/usr/bin/python3") is True
    assert pyresolve._validate_interpreter("/usr/bin/python3") is True
    assert len(calls) == 1


def test_validate_interpreter_cache_distinguishes_paths(monkeypatch):
    calls = []

    class _FakeCompleted:
        def __init__(self, rc):
            self.returncode = rc

    def _fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        return _FakeCompleted(0 if cmd[0] == "/good/python3" else 1)

    monkeypatch.setattr(pyresolve.subprocess, "run", _fake_run)

    assert pyresolve._validate_interpreter("/good/python3") is True
    assert pyresolve._validate_interpreter("/broken/python3") is False
    assert len(calls) == 2
    assert pyresolve._validate_interpreter("/good/python3") is True
    assert pyresolve._validate_interpreter("/broken/python3") is False
    assert len(calls) == 2


def test_validate_interpreter_false_result_is_cached_not_retried(monkeypatch):
    """A cached False must still surface as PythonPinInvalid on every call --
    memoizing the failure must not convert it into a silent success, and must
    not cause resolve_python_bin to silently fall through to OS-detect."""
    calls = []

    class _FakeCompleted:
        returncode = 1

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(pyresolve.subprocess, "run", _fake_run)
    monkeypatch.setattr(pyresolve, "_machine_local_get", lambda key: None)
    monkeypatch.setenv("COORDINATOR_PYTHON", "/broken/pin")

    with pytest.raises(pyresolve.PythonPinInvalid):
        pyresolve.resolve_python_bin()
    with pytest.raises(pyresolve.PythonPinInvalid):
        pyresolve.resolve_python_bin()

    assert len(calls) == 1


def test_machine_local_get_spawns_once_across_repeated_calls(monkeypatch, tmp_path):
    impl = tmp_path / "_machine_local.py"
    impl.write_text("# fake impl\n")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))

    calls = []

    class _FakeCompleted:
        returncode = 0
        stdout = "/some/python\n"

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(pyresolve.subprocess, "run", _fake_run)

    assert pyresolve._machine_local_get("coordinator.python") == "/some/python"
    assert pyresolve._machine_local_get("coordinator.python") == "/some/python"
    assert len(calls) == 1


def test_machine_local_get_cache_invalidated_by_steering_env_change(monkeypatch, tmp_path):
    """Changing MACHINE_LOCAL_IMPL (the machine-local override env this module
    reads) must not return a memoized value resolved under the old impl path --
    the cache key folds in the resolved impl path precisely so an env change
    produces a fresh cache slot instead of a stale hit."""
    impl_a = tmp_path / "impl_a.py"
    impl_a.write_text("# a\n")
    impl_b = tmp_path / "impl_b.py"
    impl_b.write_text("# b\n")

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd[1])

        class _FakeCompleted:
            returncode = 0
            stdout = f"/py/from/{cmd[1]}\n"

        return _FakeCompleted()

    monkeypatch.setattr(pyresolve.subprocess, "run", _fake_run)

    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl_a))
    first = pyresolve._machine_local_get("coordinator.python")

    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl_b))
    second = pyresolve._machine_local_get("coordinator.python")

    assert first != second
    assert len(calls) == 2


def test_clear_resolution_cache_resets_both_caches(monkeypatch):
    calls = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(pyresolve.subprocess, "run", _fake_run)

    pyresolve._validate_interpreter("/usr/bin/python3")
    assert len(calls) == 1

    pyresolve.clear_resolution_cache()

    pyresolve._validate_interpreter("/usr/bin/python3")
    assert len(calls) == 2


def test_cli_mode_resolve_is_a_cold_process_memo_is_a_noop(monkeypatch, capsys):
    """CLI mode is a fresh interpreter per invocation (spawn-per-call model) --
    the in-process memo cannot span two separate ``python3 -m
    coordinator_core.pyresolve`` processes, so within a single ``main()`` call
    the cache is populated at most once and has no observable effect versus an
    uncached resolution."""
    monkeypatch.delenv("COORDINATOR_PYTHON", raising=False)
    monkeypatch.setattr(pyresolve, "_machine_local_get", lambda key: None)
    monkeypatch.setattr(pyresolve, "_is_windows", lambda: False)
    monkeypatch.setattr(pyresolve, "_which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    # main() -> _prepend_path() does an intentionally-unguarded os.environ["PATH"]
    # write (see that function's own docstring): safe by contract only because
    # its sole real caller is this module's own subprocess-spawned __main__, which
    # exits right after. This test calls main() in-process, outside that contract,
    # so it must isolate PATH itself rather than lean on a guarantee that only
    # holds for the spawned-process caller. A same-value setenv is enough to make
    # monkeypatch snapshot and restore PATH on teardown regardless of main()'s own
    # direct write in between.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    rc = pyresolve.main(["--print-bin"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("/usr/bin/python3\t")


# ---------------------------------------------------------------------------
# _console_sibling / _WINDOWLESS_BASENAMES -- shared lift, coverage for both
# callers' shape (coordinator_core.install.substrate._resolve_baked_python_bin,
# coordinator_core.ops.ensure_python3_exe_shim._resolve_python_bin).
# ---------------------------------------------------------------------------


def test_windowless_basenames_contains_both_known_forms():
    assert pyresolve._WINDOWLESS_BASENAMES == ("pythonw.exe", "pyw.exe")


def test_console_sibling_pythonw_returns_python_exe_when_present(monkeypatch):
    windowless = r"C:\Program Files\Python313\pythonw.exe"
    probed = []
    monkeypatch.setattr(
        pyresolve.os.path, "isfile", lambda p: probed.append(p) or True
    )
    result = pyresolve._console_sibling(windowless)
    assert result == r"C:\Program Files\Python313\python.exe"
    assert result in probed


def test_console_sibling_pyw_returns_py_exe_when_present(monkeypatch):
    windowless = r"C:\Program Files\Python313\pyw.exe"
    monkeypatch.setattr(pyresolve.os.path, "isfile", lambda p: True)
    result = pyresolve._console_sibling(windowless)
    assert result == r"C:\Program Files\Python313\py.exe"


def test_console_sibling_returns_empty_when_sibling_missing(monkeypatch):
    windowless = r"C:\Program Files\Python313\pythonw.exe"
    monkeypatch.setattr(pyresolve.os.path, "isfile", lambda p: False)
    assert pyresolve._console_sibling(windowless) == ""


def test_console_sibling_returns_empty_for_non_windowless_basename():
    # Not a windowless basename at all -- no probe should even matter, "" is
    # returned before any filesystem check.
    assert pyresolve._console_sibling(r"C:\Program Files\Python313\python.exe") == ""
