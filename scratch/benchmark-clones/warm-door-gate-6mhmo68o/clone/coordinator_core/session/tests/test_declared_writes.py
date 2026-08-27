"""Tests for the DR-276 declare-write seam.

Covers the collection primitive (`session.declared_writes`) and the in-process
runner (`cli_entry.run_op_main`), which together give the in-process CLI route
the session scope-touch recording the subprocess route already had.

The dispatch-path merge in `ipc.dispatch_message` is covered at the bottom of
this file: `declare_write` and the result key are two spellings of one contract,
and the merge is what makes an op's declaration behave identically on the
subprocess route and the in-process one.

Spec backlink: docs/decisions/DR-276-operator-clis-record-session-writes-at-a.md
"""

from __future__ import annotations

import sys
import types

import pytest

from coordinator_core import cli_entry
from coordinator_core.session.declared_writes import (
    active_declarations,
    collecting,
    declare_write,
)


# --------------------------------------------------------------------------
# The collection primitive
# --------------------------------------------------------------------------


def test_declare_write_outside_a_collection_is_a_noop():
    """The pre-DR-276 behaviour is preserved for every unadopted caller."""
    assert active_declarations() is None
    declare_write("state/whatever.md")  # must not raise
    assert active_declarations() is None


def test_collecting_gathers_declarations_in_order():
    with collecting() as declared:
        declare_write("a.md")
        declare_write("b.md")
    assert declared == ["a.md", "b.md"]


def test_collection_is_closed_on_exit_even_when_the_body_raises():
    with pytest.raises(RuntimeError):
        with collecting():
            declare_write("a.md")
            raise RuntimeError("boom")
    assert active_declarations() is None


def test_nested_collections_do_not_leak_upward():
    """A handler invoking another handler must not inherit its declarations."""
    with collecting() as outer:
        declare_write("outer.md")
        with collecting() as inner:
            declare_write("inner.md")
        declare_write("outer-2.md")
    assert inner == ["inner.md"]
    assert outer == ["outer.md", "outer-2.md"]


@pytest.mark.parametrize("bad", [None, 0, b"bytes", [], object()])
def test_non_path_declarations_are_dropped_not_raised(bad):
    """Fail-open: a malformed declaration never fails an op that succeeded."""
    with collecting() as declared:
        declare_write(bad)
    assert declared == []


def test_empty_string_declaration_is_dropped():
    with collecting() as declared:
        declare_write("")
    assert declared == []


def test_pathlike_declarations_are_accepted():
    import pathlib

    with collecting() as declared:
        declare_write(pathlib.Path("state") / "x.md")
    assert declared == [str(pathlib.Path("state") / "x.md")]


def test_declarations_survive_asyncio_to_thread():
    """The dispatch path offloads sync handlers via `asyncio.to_thread`, which
    COPIES the context — appends to the bound list must still be visible."""
    import asyncio

    async def run():
        with collecting() as declared:
            await asyncio.to_thread(declare_write, "from-thread.md")
        return declared

    assert asyncio.run(run()) == ["from-thread.md"]


# --------------------------------------------------------------------------
# The in-process runner
# --------------------------------------------------------------------------


def _install_fake_op(monkeypatch, name, main):
    module = types.ModuleType(name)
    module.main = main
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_run_op_main_returns_the_ops_exit_code(monkeypatch):
    _install_fake_op(monkeypatch, "fake_op_rc", lambda argv: 3)
    assert cli_entry.run_op_main("fake_op_rc", []) == 3


def test_run_op_main_normalizes_none_to_zero(monkeypatch):
    _install_fake_op(monkeypatch, "fake_op_none", lambda argv: None)
    assert cli_entry.run_op_main("fake_op_none", []) == 0


def test_run_op_main_passes_argv_through(monkeypatch):
    seen = {}

    def main(argv):
        seen["argv"] = argv
        return 0

    _install_fake_op(monkeypatch, "fake_op_argv", main)
    cli_entry.run_op_main("fake_op_argv", ["--flag", "value"])
    assert seen["argv"] == ["--flag", "value"]


def test_run_op_main_records_declared_writes(monkeypatch):
    recorded = {}

    def main(argv):
        declare_write("state/written.md")
        return 0

    _install_fake_op(monkeypatch, "fake_op_decl", main)
    monkeypatch.setattr(
        cli_entry, "_record", lambda declared, cwd: recorded.update(paths=declared, cwd=cwd)
    )
    cli_entry.run_op_main("fake_op_decl", [], cwd="/repo")
    assert recorded == {"paths": ["state/written.md"], "cwd": "/repo"}


def test_run_op_main_records_even_when_the_op_exits_nonzero(monkeypatch):
    """A handler that wrote a file and then failed still wrote that file;
    leaving it unclaimed is the orphan this seam exists to prevent."""
    recorded = {}

    def main(argv):
        declare_write("state/partial.md")
        return 1

    _install_fake_op(monkeypatch, "fake_op_fail", main)
    monkeypatch.setattr(cli_entry, "_record", lambda declared, cwd: recorded.update(paths=declared))
    assert cli_entry.run_op_main("fake_op_fail", []) == 1
    assert recorded["paths"] == ["state/partial.md"]


def test_run_op_main_records_even_when_the_op_raises(monkeypatch):
    recorded = {}

    def main(argv):
        declare_write("state/before-boom.md")
        raise RuntimeError("boom")

    _install_fake_op(monkeypatch, "fake_op_raise", main)
    monkeypatch.setattr(cli_entry, "_record", lambda declared, cwd: recorded.update(paths=declared))
    with pytest.raises(RuntimeError):
        cli_entry.run_op_main("fake_op_raise", [])
    assert recorded["paths"] == ["state/before-boom.md"]


def test_run_op_main_raises_on_a_module_without_main(monkeypatch):
    module = types.ModuleType("fake_op_no_main")
    monkeypatch.setitem(sys.modules, "fake_op_no_main", module)
    with pytest.raises(ImportError, match="no main"):
        cli_entry.run_op_main("fake_op_no_main", [])


def test_run_op_main_routes_to_the_named_entrypoint_not_main(monkeypatch):
    """The override must not fall back to a `main` that also exists on the
    same module — that fallback is exactly the silent regression this
    parameter exists to prevent (see `install-meta-repo-precommit-hook.py`)."""
    called = {"main": False, "main_install_all": False}

    def main(argv):
        called["main"] = True
        return 1

    def main_install_all(argv):
        called["main_install_all"] = True
        return 0

    module = _install_fake_op(monkeypatch, "fake_op_two_entrypoints", main)
    module.main_install_all = main_install_all

    rc = cli_entry.run_op_main(
        "fake_op_two_entrypoints", [], entrypoint="main_install_all"
    )
    assert rc == 0
    assert called == {"main": False, "main_install_all": True}


def test_run_op_main_records_declared_writes_via_the_override_entrypoint(monkeypatch):
    recorded = {}

    def main_install_all(argv):
        declare_write("state/written-via-override.md")
        return 0

    module = types.ModuleType("fake_op_decl_override")
    module.main_install_all = main_install_all
    monkeypatch.setitem(sys.modules, "fake_op_decl_override", module)
    monkeypatch.setattr(
        cli_entry, "_record", lambda declared, cwd: recorded.update(paths=declared, cwd=cwd)
    )
    cli_entry.run_op_main(
        "fake_op_decl_override", [], cwd="/repo", entrypoint="main_install_all"
    )
    assert recorded == {"paths": ["state/written-via-override.md"], "cwd": "/repo"}


def test_run_op_main_raises_on_a_module_without_the_named_entrypoint(monkeypatch):
    module = types.ModuleType("fake_op_no_named_entrypoint")
    monkeypatch.setitem(sys.modules, "fake_op_no_named_entrypoint", module)
    with pytest.raises(ImportError, match="no main_install_all"):
        cli_entry.run_op_main(
            "fake_op_no_named_entrypoint", [], entrypoint="main_install_all"
        )


def test_run_op_main_does_not_swallow_an_unimportable_module():
    """An unresolvable op module is a transport failure the trampoline reports,
    never something silently converted into a success."""
    with pytest.raises(ImportError):
        cli_entry.run_op_main("coordinator_core.ops.definitely_not_a_real_op", [])


def test_recording_declared_writes_records_on_clean_exit(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        cli_entry, "_record", lambda declared, cwd: recorded.update(paths=declared, cwd=cwd)
    )
    with cli_entry.recording_declared_writes(cwd="/repo") as declared:
        declare_write("state/via-helper.md")
        assert declared == ["state/via-helper.md"]
    assert recorded == {"paths": ["state/via-helper.md"], "cwd": "/repo"}


def test_recording_declared_writes_records_on_an_exception_escaping_the_body(monkeypatch):
    recorded = {}
    monkeypatch.setattr(cli_entry, "_record", lambda declared, cwd: recorded.update(paths=declared))
    with pytest.raises(RuntimeError):
        with cli_entry.recording_declared_writes(cwd="/repo"):
            declare_write("state/before-helper-boom.md")
            raise RuntimeError("boom")
    assert recorded["paths"] == ["state/before-helper-boom.md"]


def test_recording_declared_writes_is_a_noop_when_nothing_was_declared(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        "coordinator_core.ipc._record_self_reported_touches",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    with cli_entry.recording_declared_writes(cwd="/repo"):
        pass
    assert called["n"] == 0


def test_record_is_fail_open(monkeypatch):
    """Recording failure must never fail an op that already succeeded."""

    def exploding_recorder(*_a, **_kw):
        raise OSError("session dir unwritable")

    monkeypatch.setattr(
        "coordinator_core.ipc._record_self_reported_touches", exploding_recorder
    )
    cli_entry._record(["state/x.md"], "/repo")  # must not raise


def test_record_skips_the_import_entirely_when_nothing_was_declared(monkeypatch):
    """The common case (no adoption) must not pay for importing ipc."""
    called = {"n": 0}
    monkeypatch.setattr(
        "coordinator_core.ipc._record_self_reported_touches",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    cli_entry._record([], "/repo")
    assert called["n"] == 0


# --------------------------------------------------------------------------
# The dispatch-path merge
# --------------------------------------------------------------------------


def test_ipc_merges_declare_write_into_the_self_report_key():
    """`declare_write` and the result key are two spellings of one contract, so
    an op may use either or both and the recorder sees a single deduped list."""
    import asyncio

    from coordinator_core import ipc

    seen = {}

    def handler(params, repo_root=None):
        declare_write("state/from-call.md")
        declare_write("state/dup.md")
        return {"ok": True, ipc._SCOPE_TOUCH_PATHS_KEY: ["state/dup.md", "state/from-key.md"]}

    ipc.register_op("test.declare_merge", handler)
    try:
        original = ipc._record_self_reported_touches

        def spy(result, cwd):
            seen["paths"] = list(result.get(ipc._SCOPE_TOUCH_PATHS_KEY) or [])
            return original(result, cwd)

        ipc._record_self_reported_touches = spy
        try:
            asyncio.run(
                ipc.dispatch_message(
                    {"jsonrpc": "2.0", "id": 1, "method": "test.declare_merge", "params": {}}
                )
            )
        finally:
            ipc._record_self_reported_touches = original
    finally:
        ipc._REGISTRY.pop("test.declare_merge", None)

    assert seen["paths"] == [
        "state/dup.md",
        "state/from-key.md",
        "state/from-call.md",
    ], "key-declared paths keep their order and precede call-declared ones; dupes dropped"
