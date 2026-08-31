"""test_cc_invoke_warm_reach_degrades_without_engine.py — the warm preamble
degrades to the cold spawn instead of aborting the caller.

`_try_in_process_warm_reach` opens with three `coordinator_core` imports
(`warm.settings`, `op_scopes`, `warm.client`). Its docstring asserted "Never
raises", reasoning from `try_warm_dispatch` never raising — which covers the
DISPATCH CALL and says nothing about the imports that precede it. On a box with
no `coordinator_core` on the interpreter's import graph, the first of those
imports raised and took the caller with it: three of eight `/workday-start`
addon-health probes (`check-engine-drift.py`, `check-deferral-orphan-memo.py`,
`check-deferral-partial-strangle.py`) hard-crashed on
`ModuleNotFoundError: No module named 'coordinator_core'`, while every other
engine CLI in the same ceremony completed fine under that same interpreter.

Warmth is an optimisation with a cold spawn underneath it. An interpreter that
cannot import the engine in-process must take that spawn, so a miss here is
`None` — the same value every other miss returns — and never an exception.

This test does NOT assert warmth works. It asserts the failure mode when it
cannot be reached, which is the only thing the reported crash was about.

Reported: cross-repo/inbox/2026-08-31-doe-claude-em-engine-cc-invoke-warm-import-crash.md
Baton: state/handoffs/2026-08-31-fleet-python-import-topology-the-editabl.md § D

Run: pytest coordinator/bin/tests/test_cc_invoke_warm_reach_degrades_without_engine.py -q
"""
from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"


@pytest.fixture(scope="module")
def cc_invoke_mod():
    """Load `cc_invoke` by location, with `bin/lib` on `sys.path` for its own
    bare sibling imports (`engine_bootstrap` and friends)."""
    if str(_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_LIB_DIR))
    spec = importlib.util.spec_from_file_location(
        "_cc_invoke_under_test", _LIB_DIR / "cc_invoke.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _block(monkeypatch, blocked_prefix: str) -> None:
    """Make every `coordinator_core...` import under `blocked_prefix` raise
    ImportError, exactly as a bare interpreter with no engine would."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == blocked_prefix or name.startswith(blocked_prefix + "."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for mod_name in list(sys.modules):
        if mod_name == blocked_prefix or mod_name.startswith(blocked_prefix + "."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)


def test_no_engine_at_all_returns_none(cc_invoke_mod, monkeypatch):
    """The reported crash: nothing under `coordinator_core` is importable."""
    _block(monkeypatch, "coordinator_core")
    assert cc_invoke_mod._try_in_process_warm_reach("engine.drift", {}, ".") is None


@pytest.mark.parametrize(
    "blocked",
    [
        "coordinator_core.warm.settings",
        "coordinator_core.op_scopes",
        "coordinator_core.warm.client",
    ],
)
def test_each_import_site_degrades_independently(cc_invoke_mod, monkeypatch, blocked):
    """Each of the three sites carries its own guard.

    The justification differs per site and is not uniform — the first draft of
    this docstring claimed "two different subpackages" for all three, which is
    only true of the first two (Kira, 2026-08-31):

      1. `coordinator_core.warm.settings` — the real reported failure. Nothing
         has been proven importable yet when this runs.
      2. `coordinator_core.op_scopes` — a DIFFERENT subpackage from (1), so (1)
         succeeding proves nothing about it. This guard is load-bearing.
      3. `coordinator_core.warm.client` — same subpackage as (1), so (1)
         succeeding does make an ImportError here unlikely. Kept anyway, and
         honestly labelled: it is belt-and-braces against a partially-installed
         or partially-shadowed `warm` package, not a distinct failure anyone has
         seen. Cheap, and the alternative is an unguarded import in a function
         whose contract is "never raises".
    """
    _block(monkeypatch, blocked)
    assert cc_invoke_mod._try_in_process_warm_reach("engine.drift", {}, ".") is None


def test_a_non_import_error_still_propagates(cc_invoke_mod, monkeypatch):
    """Negative spec: the guards are narrowed to ImportError on the import
    line, not a blanket swallow. A defect inside `is_warm_enabled()` is a
    real defect and must not be laundered into a silent cold-spawn."""
    import coordinator_core.warm.settings as settings_mod

    def boom():
        raise RuntimeError("registry read failed")

    monkeypatch.setattr(settings_mod, "is_warm_enabled", boom)
    with pytest.raises(RuntimeError, match="registry read failed"):
        cc_invoke_mod._try_in_process_warm_reach("engine.drift", {}, ".")
