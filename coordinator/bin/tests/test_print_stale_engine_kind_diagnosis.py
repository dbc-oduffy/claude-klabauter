"""test_print_stale_engine_kind_diagnosis — coverage for the publish-lag
diagnosis helper in `cross-repo-memo.py`.

The regex/set-difference
diagnosis logic (`_print_stale_engine_kind_diagnosis`) had no test, so a
regression in the engine's refusal-message format (or in the paren-group
parsing) would silently stop firing with no signal.

Run: python -m pytest coordinator/bin/tests/test_print_stale_engine_kind_diagnosis.py -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import pathlib
from contextlib import redirect_stderr

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "cross_repo_memo_diagnosis", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo_diagnosis", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_diagnosis_fires_on_kind_missing_from_served_engine():
    mod = _load_cli_module()
    assert "notice" in mod._VALID_KINDS, (
        "fixture assumes 'notice' is a CLI-accepted kind; update the probe "
        "kind if _VALID_KINDS changes"
    )

    class _Refused(BaseException):
        op_stderr = (
            "kind 'notice' is not a valid enum value (must be one of: "
            "ask, consult, fyi, proposal, bug)"
        )

    buf = io.StringIO()
    with redirect_stderr(buf):
        mod._print_stale_engine_kind_diagnosis(_Refused("op failed"))

    out = buf.getvalue()
    assert "PUBLISH LAG" in out
    assert "'notice'" in out


def test_diagnosis_silent_on_ordinary_refusal():
    mod = _load_cli_module()

    class _Refused(BaseException):
        op_stderr = "publish_target_rejected: no such receiver"

    buf = io.StringIO()
    with redirect_stderr(buf):
        mod._print_stale_engine_kind_diagnosis(_Refused("op failed"))

    assert buf.getvalue() == ""


def test_diagnosis_silent_when_served_set_already_contains_the_kind():
    mod = _load_cli_module()

    class _Refused(BaseException):
        op_stderr = (
            "kind 'fyi' is not a valid enum value (must be one of: "
            "ask, consult, fyi, proposal, bug)"
        )

    buf = io.StringIO()
    with redirect_stderr(buf):
        mod._print_stale_engine_kind_diagnosis(_Refused("op failed"))

    assert buf.getvalue() == ""
