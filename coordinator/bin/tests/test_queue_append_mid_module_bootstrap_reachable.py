from __future__ import annotations
"""
test_queue_append_mid_module_bootstrap_reachable.py — regression test for
coordinator-queue-append's mid-module deferred-bootstrap coverage.

Spec backlink: this repo's 2026-08-29 review-finding sweep over the
lazy-bootstrap pass on `coordinator/bin/*.py`, Finding 3 (MAJOR).

Six sibling functions (`_current_repo_root`, `_resolve_session_id`,
`_output_path`, `_write_out_path_excl`, `_schema_cli_describe`,
`_schema_cli_validate`) read names `_bootstrap_imports()` binds as bare
module globals, but only `main()` called `_bootstrap_imports()` — so any
entry into this module that reaches one of these functions WITHOUT first
calling `main()` (a test, or `workstream_complete.apply._load_cli_module`'s
in-process dispatch reaching a helper function directly) raised `NameError`.
The module's PEP 562 `__getattr__` hook does not help here: it only covers
module-ATTRIBUTE access from an importer, never a bare-name lookup inside the
module's own function bodies (see this file's own `__getattr__`
docstring — it exists for `cli._cc_route`-shaped external access, not for
calls originating inside this module).

This test loads the module fresh and calls each of the six functions'
lowest-dependency form directly — never via `main()` — asserting none raises
`NameError`. A real invocation of most of these needs a git repo / registry
context this suite does not want to depend on, so each call is wrapped to
tolerate any exception EXCEPT `NameError`: a `RuntimeError`/`SystemExit` from
missing environment context is an unrelated, expected failure mode; a
`NameError` is specifically the invisible-to-`--help`/import/py_compile
defect this finding names.

Run with: python3 -m pytest test_queue_append_mid_module_bootstrap_reachable.py
"""

import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

import pytest

pytestmark = [pytest.mark.cadence]

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)  # coordinator/bin
_QUEUE_APPEND_CLI = os.path.join(_BIN_DIR, "coordinator-queue-append.py")

_MODULE_NAME = "test_coordinator_queue_append_mid_module_module"


def _load_fresh_module():
    """Load coordinator-queue-append fresh, without running `main()` -- the
    exact entry shape a mid-module caller (or in-process dispatch reaching a
    helper directly) presents.
    """
    sys.modules.pop(_MODULE_NAME, None)
    loader = SourceFileLoader(_MODULE_NAME, _QUEUE_APPEND_CLI)
    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, _QUEUE_APPEND_CLI, loader=loader
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._BOOTSTRAP_DONE is False, (
        "test setup invariant broken: module bootstrapped itself at import time"
    )
    return module


def _assert_no_name_error(label: str, fn) -> None:
    try:
        fn()
    except NameError as exc:  # pragma: no cover - failure path under test
        pytest.fail(f"{label} raised NameError on mid-module entry: {exc!r}")
    except SystemExit:
        pass
    except Exception:
        # Any other failure (missing git repo, registry unresolved, etc.) is an
        # unrelated environment precondition, not the defect this test targets.
        pass


def test_current_repo_root_reachable_without_main() -> None:
    module = _load_fresh_module()
    _assert_no_name_error("_current_repo_root", module._current_repo_root)


def test_resolve_session_id_reachable_without_main() -> None:
    module = _load_fresh_module()
    _assert_no_name_error("_resolve_session_id", module._resolve_session_id)


def test_output_path_reachable_without_main() -> None:
    module = _load_fresh_module()
    _assert_no_name_error(
        "_output_path",
        lambda: module._output_path("debt-backlog", "a test title"),
    )


def test_write_out_path_excl_reachable_without_main() -> None:
    module = _load_fresh_module()
    _assert_no_name_error(
        "_write_out_path_excl",
        lambda: module._write_out_path_excl(
            os.path.join(_THIS_DIR, "__does_not_exist__", "x.yaml"), "x"
        ),
    )


def test_schema_cli_describe_reachable_without_main() -> None:
    module = _load_fresh_module()
    _assert_no_name_error(
        "_schema_cli_describe", lambda: module._schema_cli_describe("debt-backlog")
    )


def test_schema_cli_validate_reachable_without_main() -> None:
    module = _load_fresh_module()
    _assert_no_name_error(
        "_schema_cli_validate",
        lambda: module._schema_cli_validate("debt-backlog", {}),
    )
