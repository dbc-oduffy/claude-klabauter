"""test_cutover_cli_empty_verdict_notes.py — pytest coverage of cutover-cli's
empty-verdict_line path in `_run_gate_shaped_op`.

2026-07-25 break-class fix: `_run_gate_shaped_op` printed only
"coordinator_core returned empty verdict_line" and returned 1 when
`verdict_line` came back empty, discarding `result["notes"]` — the exact
field every `cutover.advance` abort path (D4 refusal ladder) puts its actual
reason in. The `exit_code == 2` branch a few lines above already prints
notes unconditionally; this fix mirrors that on the empty-verdict path too.

Spec backlink: cross-repo/inbox/2026-07-25-coordinator-claude-em-posix-bareword-path-provisioning.md

Hermeticity follows `test_close_origin_stub_on_ship.py`'s established
pattern: `cutover-cli` does `sys.path.insert(0, lib_dir); import cc_invoke`
before anything else, so pre-seeding `sys.modules["cc_invoke"]` with a fake
`route` short-circuits Python's import machinery before any real
CLAUDE_KLABAUTER_ROOT / coordinator_core.invoke subprocess is ever spawned. Each test
loads a fresh copy of the subject module so per-test fakes never leak.
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
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "cutover-cli")


_ABSENT = object()


def _install_fake_cc_invoke(route_fn):
    """Seed sys.modules["cc_invoke"] with a fake module exposing `route` and
    `_resolve_claude_klabauter_root`. Must run BEFORE the subject module is imported —
    `cutover-cli` does `import cc_invoke; from cc_invoke import
    _resolve_claude_klabauter_root`, both of which resolve against sys.modules first.

    Returns the prior sys.modules entry (or `_ABSENT`) — hand it to
    `_restore_cc_invoke` in a `finally`.

    Negative spec: `sys.modules["cc_invoke"]` is process-global, and 30+
    `coordinator/bin/` scripts import `cc_invoke` by bare name. A fake left
    installed past the test that seeded it makes every later such import in the
    same worker resolve against a module carrying only these two attributes;
    the missing name surfaces as an ImportError at the victim's fixture setup,
    which pytest reports as an ERROR in an unrelated file. Never install
    without a paired restore."""
    fake = types.ModuleType("cc_invoke")
    fake.route = route_fn
    fake._resolve_claude_klabauter_root = lambda: "/fake/claude-klabauter/root"
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    """Undo `_install_fake_cc_invoke`, restoring absence as absence."""
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    """Import a brand-new copy of the subject module so per-test route fakes
    never leak across tests.

    `cutover-cli` has no `.py` extension — `spec_from_file_location` can't
    infer a loader for it, leaving `spec.loader is None` (mirrors
    `test_cross_repo_memo.py`'s `_load_module`), so an explicit
    `SourceFileLoader` is required.
    """
    from importlib.machinery import SourceFileLoader

    sys.modules.pop("cutover-cli", None)
    loader = SourceFileLoader("cutover-cli", SUBJECT_PATH)
    spec = importlib.util.spec_from_loader("cutover-cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run_show(route_fn, record_path):
    prior_cc_invoke = _install_fake_cc_invoke(route_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject._cmd_show([record_path])
    finally:
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


def test_empty_verdict_line_prints_notes_to_stderr():
    def _route(op, params, repo_root, legacy_fn):
        return {
            "verdict_line": "",
            "notes": ["cutover.gate: record has no confirmed_consumers entries"],
            "exit_code": 1,
        }

    code, out, err = _run_show(_route, "state/roadmap/lifecycle-vocab/cutovers/demo.md")
    assert code == 1
    assert "coordinator_core returned empty verdict_line" in err
    assert "cutover.gate: record has no confirmed_consumers entries" in err


def test_empty_verdict_line_with_no_notes_does_not_crash():
    def _route(op, params, repo_root, legacy_fn):
        return {"verdict_line": "", "notes": [], "exit_code": 1}

    code, out, err = _run_show(_route, "state/roadmap/lifecycle-vocab/cutovers/demo.md")
    assert code == 1
    assert "coordinator_core returned empty verdict_line" in err


def test_nonempty_verdict_line_still_prints_verdict_and_notes():
    def _route(op, params, repo_root, legacy_fn):
        return {
            "verdict_line": "COVERAGE_OK",
            "notes": ["some informational note"],
            "exit_code": 0,
        }

    code, out, err = _run_show(_route, "state/roadmap/lifecycle-vocab/cutovers/demo.md")
    assert code == 0
    assert "COVERAGE_OK" in out
    assert "some informational note" in err
