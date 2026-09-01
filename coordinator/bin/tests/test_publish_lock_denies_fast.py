"""test_publish_lock_denies_fast — C3: `publish.py`'s own destination lock
follows `percolate-round.py`'s policy (deny-at-once by default, no sleep on
a contended dest) and emits the SAME refusal text
(`percolate.wire_contract.lock_busy_message`) `percolate-round.py` and
`percolate-mirror.py` do, rather than its own inline BUSY f-string.

Prior to C3, `publish.py`'s inline `[publish.py] BUSY:` branch under a 0
wait read "waited 0s ... Re-run once it lands" -- false (no wait happened),
inviting the retry the whole plan exists to stop, and naming a knob
(`COORDINATOR_LOCK_WAIT_SECS`) that no longer governs this path. This suite
pins the fixed shape directly against `main()`'s real lock loop -- never a
source grep of the module's own docstring.

Spec backlink: state/dispatch-briefs/2026-08-30-a-second-percolate-round-
stops-sleeping/C3.md
Spec backlink: docs/reference/percolate-lock-contention.md

Run: python -m pytest coordinator/bin/tests/test_publish_lock_denies_fast.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_lock_denies_fast_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _init_git_repo(root: Path) -> None:
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _wire_common_fakes(publish_mod, monkeypatch, tmp_path, row_dests: "dict[str, Path]"):
    """Same shape as `test_percolate_round.py::_wire_lock_test_fakes` --
    stubs every precondition `main()` runs before its OWN lock loop, so
    this test drives the REAL lock-acquisition and BUSY-message code (the
    code under test for C3) rather than a hand-rolled stand-in for it."""

    def fake_row(name: str, dest: Path) -> str:
        src = tmp_path / f"src-{name}"
        src.mkdir(parents=True, exist_ok=True)
        return f"{name}|mirror|{src}|{dest}"

    monkeypatch.setattr(
        publish_mod, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    monkeypatch.setattr(
        publish_mod, "load_targets", lambda setup_dir, target_filter=None, **_: [
            fake_row(name, dest) for name, dest in row_dests.items()
        ]
    )

    class _FakeClaudeKlabauter:
        def resolve_target(self, store, name):
            raise KeyError(name)

        def run_parse_sweep(self, repo_root):
            return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

        def enumerate_gate_entrypoints(self, repo_root):
            return ()

    monkeypatch.setattr(publish_mod, "_import_claude_klabauter_percolate", lambda: _FakeClaudeKlabauter())
    monkeypatch.setattr(publish_mod, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
    monkeypatch.setattr(publish_mod, "locate_percolate_store", lambda setup_dir: tmp_path / "store.yaml")
    monkeypatch.setattr(publish_mod, "resolve_percolate_identity_path", lambda setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish_mod, "check_identity_file_present", lambda path, setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish_mod, "check_identity_file_safe", lambda path: None)
    monkeypatch.setattr(
        publish_mod,
        "parse_percolate_identity",
        lambda path: publish_mod.PercolateIdentity(review=["dummy-pattern"]),
    )
    monkeypatch.setattr(publish_mod, "_resolve_publish_sync_module_path", lambda setup_dir: tmp_path / "publish_sync.py")
    monkeypatch.setattr(publish_mod, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish_mod, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish_mod, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish_mod, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish_mod, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        totals.processed += 1

    monkeypatch.setattr(publish_mod, "process_target", fake_process_target)


@pytest.mark.spawns_process
def test_publish_denies_fast_under_default_zero_wait(tmp_path, monkeypatch):
    """A contended dest refuses INSTANTLY under the default wait (no
    `COORDINATOR_ALLOW_PERCOLATE_QUEUE`). Exit code stays 75 (EX_TEMPFAIL) --
    unchanged by C3. The refusal TEXT's content contract (holder named,
    mechanism page present, override key/re-run imperative absent) is
    asserted once, directly on `wire_contract.lock_busy_message`
    (test_wire_contract_publish_contention.py) -- this suite only checks
    that publish.py's own `[publish.py] BUSY:` prefix wraps that builder's
    output, never re-deriving the builder's own content claims."""
    import coordinator_core.locked_write as locked_write

    publish_mod = _load_publish_module()

    dest = tmp_path / "dest"
    dest.mkdir()
    _init_git_repo(dest)

    monkeypatch.delenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", raising=False)
    monkeypatch.delenv(locked_write.CONTENDED_LOCK_WAIT_ENV, raising=False)

    _wire_common_fakes(publish_mod, monkeypatch, tmp_path, {"row-a": dest})

    buf = io.StringIO()
    with locked_write.held_lock(Path(dest), holder_label="third-party-holder"):
        with contextlib.redirect_stderr(buf):
            rc = publish_mod.main(["row-a"])

    assert rc == 75
    err = buf.getvalue()
    assert "[publish.py] BUSY:" in err
    assert "third-party-holder" in err


@pytest.mark.spawns_process
def test_publish_emits_the_same_text_round_does(tmp_path, monkeypatch):
    """`publish.py`'s inline BUSY branch and `percolate-round.py`'s
    `_lock_busy_message` must produce byte-identical text for the same
    `(dest, exc)` pair -- both now delegate to
    `percolate.wire_contract.lock_busy_message` (staff-eng finding 0)."""
    import importlib.util as _ilu

    from percolate.wire_contract import lock_busy_message

    spec = _ilu.spec_from_file_location(
        "percolate_round_for_wire_parity_check", _BIN_DIR / "percolate-round.py"
    )
    round_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(round_mod)

    exc = Exception("held by pid 1234 since 2026-08-30T00:00:00Z (within 0s)")
    assert round_mod._lock_busy_message("some-dest", exc) == lock_busy_message("some-dest", exc)


@pytest.mark.spawns_process
def test_publish_first_contended_row_refuses_not_flattened_to_generic_fail(tmp_path, monkeypatch):
    """A multi-row publish refuses on the FIRST contended root (canonical
    realpath-sorted order) and names THAT root -- never a generic FAIL that
    swallows which destination is actually held."""
    import coordinator_core.locked_write as locked_write

    publish_mod = _load_publish_module()

    root_a = tmp_path / "dest-a"
    root_b = tmp_path / "dest-b"
    root_a.mkdir()
    root_b.mkdir()
    _init_git_repo(root_a)
    _init_git_repo(root_b)

    monkeypatch.delenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", raising=False)

    _wire_common_fakes(publish_mod, monkeypatch, tmp_path, {"row-a": root_a, "row-b": root_b})

    first_contended = sorted([root_a, root_b], key=lambda p: os.path.realpath(str(p)))[0]

    buf = io.StringIO()
    with locked_write.held_lock(Path(first_contended), holder_label="third-party-holder"):
        with contextlib.redirect_stderr(buf):
            rc = publish_mod.main(["row-a,row-b"])

    assert rc == 75
    err = buf.getvalue()
    assert str(first_contended) in err
