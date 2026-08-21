"""test_promote-shipped-in-flight-stubs.py — pytest suite for
promote-shipped-in-flight-stubs.py's exit-code propagation.

Review: code-reviewer Finding 5 — the trampoline's docstring changed from a
hardcoded "Exit codes: 0 always" contract to "propagated verbatim ... see
that module's own docstring for the AC14 split" (a real behavior change at
this layer; DoE's `/workday-start` reads this process's exit code), but the
propagation itself was not independently asserted anywhere in that diff. The
ops-module layer (coordinator_core/ops/test_promote_shipped_in_flight_stubs.py)
is well tested; this file closes the trampoline-layer gap.

Hermeticity: fakes `cc_invoke._resolve_claude_klabauter_root` (seeded into
`sys.modules["cc_invoke"]` before import, mirroring
test_close_origin_stub_on_ship.py's pattern) so no real engine-root
resolution runs, and fakes
`coordinator_core.ops.promote_shipped_in_flight_stubs` directly so `main()`'s
return value is fully controlled — the assertion is ONLY "does the
trampoline's sys.exit() carry the ops-module's return value through
unchanged", not the ops-module's own AC14 business logic (already covered
elsewhere).

Spec backlink: pln-terminal-state-propagation-giv-c85539
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "promote-shipped-in-flight-stubs.py")

_ABSENT = object()


def _install_fakes(op_main_fn):
    """Seed sys.modules with a fake `cc_invoke` (so `_resolve_claude_klabauter_root`
    never does real engine-root resolution) and a fake
    `coordinator_core.ops.promote_shipped_in_flight_stubs` (so `main()`'s
    return value is fully controlled). Returns the prior sys.modules entries
    for both names — hand them to `_restore_fakes` in a `finally`.
    """
    fake_cc_invoke = types.ModuleType("cc_invoke")
    fake_cc_invoke._resolve_claude_klabauter_root = lambda: "/nonexistent/fake-claude-klabauter-root"
    prior_cc_invoke = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake_cc_invoke

    # The subject's `_import_main` also does `from coordinator_core.cli_entry
    # import recording_declared_writes` (DR-276 conversion). That is real,
    # always-present production code — not the seam under test here (only
    # the ops module's return value is) — so it is imported for real and
    # spliced onto the fake `coordinator_core` package rather than left
    # absent. A plain `types.ModuleType("coordinator_core")` has no
    # `__path__`, so Python's import machinery cannot resolve
    # `coordinator_core.cli_entry` as a submodule through it and raises
    # ModuleNotFoundError — caught by the subject's own `except ImportError`
    # and silently collapsed to exit 0, before `repo_root`/exit-code are
    # ever threaded through to the ops module. Registering the real
    # `coordinator_core.cli_entry` module directly in `sys.modules` sides
    # around the missing-`__path__` problem without needing to fake it.
    import coordinator_core.cli_entry as _real_cli_entry

    fake_pkg = types.ModuleType("coordinator_core")
    fake_ops_pkg = types.ModuleType("coordinator_core.ops")
    fake_op_module = types.ModuleType("coordinator_core.ops.promote_shipped_in_flight_stubs")
    fake_op_module.main = op_main_fn
    fake_pkg.ops = fake_ops_pkg
    fake_pkg.cli_entry = _real_cli_entry
    fake_ops_pkg.promote_shipped_in_flight_stubs = fake_op_module

    prior_pkg = sys.modules.get("coordinator_core", _ABSENT)
    prior_ops_pkg = sys.modules.get("coordinator_core.ops", _ABSENT)
    prior_cli_entry = sys.modules.get("coordinator_core.cli_entry", _ABSENT)
    prior_op_module = sys.modules.get(
        "coordinator_core.ops.promote_shipped_in_flight_stubs", _ABSENT
    )
    sys.modules["coordinator_core"] = fake_pkg
    sys.modules["coordinator_core.ops"] = fake_ops_pkg
    sys.modules["coordinator_core.cli_entry"] = _real_cli_entry
    sys.modules["coordinator_core.ops.promote_shipped_in_flight_stubs"] = fake_op_module

    return {
        "cc_invoke": prior_cc_invoke,
        "coordinator_core": prior_pkg,
        "coordinator_core.ops": prior_ops_pkg,
        "coordinator_core.cli_entry": prior_cli_entry,
        "coordinator_core.ops.promote_shipped_in_flight_stubs": prior_op_module,
    }


def _restore_fakes(prior: dict) -> None:
    for name, value in prior.items():
        if value is _ABSENT:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value


def _load_subject_fresh():
    sys.modules.pop("promote-shipped-in-flight-stubs", None)
    spec = importlib.util.spec_from_file_location(
        "promote-shipped-in-flight-stubs", SUBJECT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(op_main_fn):
    """Load a fresh subject with the given fake op main(), call its main(),
    and capture the SystemExit code plus stdout/stderr."""
    prior = _install_fakes(op_main_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with pytest.raises(SystemExit) as exc_info:
                subject.main()
    finally:
        _restore_fakes(prior)
    return exc_info.value.code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


def test_nonzero_exit_propagates_unchanged():
    """The one case Finding 5 names explicitly: a non-zero ops-module return
    (AC14's stamp_abort_count > 0 loud path) must flow through this
    trampoline's sys.exit() unchanged, not collapse to 0."""
    code, out, err = _run_main(lambda argv, repo_root=None: 1)
    assert code == 1


def test_zero_exit_propagates_unchanged():
    code, out, err = _run_main(lambda argv, repo_root=None: 0)
    assert code == 0


def test_repo_root_passed_script_dir_relative():
    """repo_root is derived from THIS file's own grandparent directory
    (SCRIPT_DIR-relative), not the invoking shell's cwd — see subject
    module docstring "Usage"."""
    captured = {}

    def _fake_main(argv, repo_root=None):
        captured["repo_root"] = repo_root
        return 0

    code, out, err = _run_main(_fake_main)
    assert code == 0
    expected_repo_root = os.path.dirname(os.path.dirname(SCRIPT_DIR))
    assert captured["repo_root"] == expected_repo_root
