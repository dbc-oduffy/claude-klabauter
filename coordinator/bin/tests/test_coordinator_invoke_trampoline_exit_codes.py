"""coordinator/bin/tests/test_coordinator_invoke_trampoline_exit_codes.py

Claude-klabauter#31: a caller checking "is an engine reachable at all" needs a
fast, cheap, distinguishable answer — this pins `coordinator-invoke.py`'s own
exit-code contract for the case the resolver bottoms out.

Loaded via `spec_from_file_location`, matching `coordinator/bin`'s ad-hoc
script-loading convention (see `bin_git_free_seam.py`'s own docstring):
`coordinator-invoke.py`'s hyphenated stem is not an importable module name.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = BIN_DIR / "coordinator-invoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_coordinator_invoke_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolution_failure_exits_2_and_names_a_runnable_fix(monkeypatch, capsys):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_import_main",
        lambda: (_ for _ in ()).throw(RuntimeError("every rung missed")),
    )

    rc = module.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "scripts/cloud_setup.py" in err
    assert "scripts/setup.py" in err
    assert "/coordinator:" not in err, "cold-path remediation must never name a slash command"


def test_import_error_still_exits_1_distinct_from_resolution_failure(monkeypatch, capsys):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_import_main",
        lambda: (_ for _ in ()).throw(ImportError("coordinator_core.invoke.__main__ missing")),
    )

    rc = module.main([])

    assert rc == 1
    err = capsys.readouterr().err
    assert "not importable" in err


def test_unrelated_exception_from_import_main_is_not_silently_coerced(monkeypatch):
    """The except clauses above are narrowly scoped to RuntimeError/ImportError
    by name -- a third, unrelated exception type (e.g. a bug inside
    `_import_main` itself) must propagate rather than falling through to some
    other exit code, since the exit-code contract this file pins is now
    load-bearing for callers using `ping` as a reachability probe."""
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_import_main",
        lambda: (_ for _ in ()).throw(AttributeError("unrelated bug")),
    )

    with pytest.raises(AttributeError):
        module.main([])


def test_ping_is_the_cheapest_reachability_probe_and_needs_no_new_flag():
    """claude-klabauter#31 Q2: `ping` (coordinator_core.ops.ping) is already the
    cheapest registered op, and this trampoline resolves the engine root on
    EVERY invocation before any op-specific code runs — so `coordinator-invoke
    ping '{}'`'s exit code already answers "is an engine reachable at all"
    with no new flag, no new file, and no provisioning side effect (project-
    claude-klabauter#31 Q1, PM-ruled: provisioning stays scripts/cloud_setup.py's job).
    """
    module = _load_module()
    import inspect

    src = inspect.getsource(module.main)
    # `_import_main()` (the resolution step) runs unconditionally, ahead of
    # any op dispatch — there is no argv branch that skips it for a cheap op.
    assert "op_main = _import_main()" in src
