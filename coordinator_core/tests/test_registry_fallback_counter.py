"""The registry-fallback counter must make the map-miss perf cliff visible
WITHOUT ever becoming a way for dispatch to fail.

Both halves are load-bearing and neither is obvious from the module alone:
the counter is worthless if the fast path records (it would drown the signal
it exists to raise), and actively harmful if a telemetry write failure can
stop an op resolving.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import ipc, registry_fallback_counter as fbc

_FILENAME = "registry-fallback-counts.jsonl"


def _counts_path(root: Path, sid: str) -> Path:
    return root / "state" / "subagent-share" / sid / _FILENAME


def _write_one(tmp_path: Path, monkeypatch, **kwargs) -> Path:
    monkeypatch.setattr(fbc, "resolve_git_root_cheap", lambda cwd=None: str(tmp_path))
    payload = {"method": "x.y", "stage": fbc.STAGE_SAFE_FALLBACK, "session_id": "sid-1", "mapped": False}
    payload.update(kwargs)
    fbc.record_registry_fallback(**payload)
    return _counts_path(tmp_path, payload["session_id"])


def test_records_the_op_stage_and_mapped_flag(tmp_path, monkeypatch):
    path = _write_one(tmp_path, monkeypatch, method="ceremony.thing", mapped=True)
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["op"] == "ceremony.thing"
    assert record["stage"] == fbc.STAGE_SAFE_FALLBACK
    assert record["mapped"] is True
    assert record["at"]


def test_mapped_flag_separates_absent_entry_from_stale_one(tmp_path, monkeypatch):
    """The two causes want different repairs, so the record must tell them apart:
    absent from the map (add an entry) vs present but not registering (the
    module is failing to import, which is silent everywhere else too)."""
    monkeypatch.setattr(fbc, "resolve_git_root_cheap", lambda cwd=None: str(tmp_path))
    fbc.record_registry_fallback("a.absent", fbc.STAGE_SAFE_FALLBACK, "sid-1", mapped=False)
    fbc.record_registry_fallback("b.stale", fbc.STAGE_SAFE_FALLBACK, "sid-1", mapped=True)
    records = [json.loads(l) for l in _counts_path(tmp_path, "sid-1").read_text(encoding="utf-8").splitlines()]
    assert {r["op"]: r["mapped"] for r in records} == {"a.absent": False, "b.stale": True}


def test_appends_rather_than_truncating(tmp_path, monkeypatch):
    monkeypatch.setattr(fbc, "resolve_git_root_cheap", lambda cwd=None: str(tmp_path))
    for i in range(3):
        fbc.record_registry_fallback(f"op.{i}", fbc.STAGE_SAFE_FALLBACK, "sid-1", mapped=False)
    lines = _counts_path(tmp_path, "sid-1").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


@pytest.mark.parametrize("sid", ["", None])
def test_no_op_on_unresolvable_session_id(tmp_path, monkeypatch, sid):
    monkeypatch.setattr(fbc, "resolve_git_root_cheap", lambda cwd=None: str(tmp_path))
    fbc.record_registry_fallback("x.y", fbc.STAGE_SAFE_FALLBACK, sid or "", mapped=False)
    assert not (tmp_path / "state").exists()


def test_no_op_on_unresolvable_git_root_rather_than_a_shared_fallback_path(tmp_path, monkeypatch):
    """Inventing a fallback path would recreate the cross-session concurrency
    hazard the per-session file exists to avoid."""
    monkeypatch.setattr(fbc, "resolve_git_root_cheap", lambda cwd=None: None)
    fbc.record_registry_fallback("x.y", fbc.STAGE_SAFE_FALLBACK, "sid-1", mapped=False)
    assert not (tmp_path / "state").exists()


# --- the wiring in ipc.py -------------------------------------------------


def test_stage_literal_in_ipc_matches_the_counter_constant():
    """ipc.py passes the stage name as a literal at its one call site; if it
    drifts from this module's constant the records become unreadable by the
    very reducer they exist for, and nothing else would catch it."""
    source = Path(ipc.__file__).read_text(encoding="utf-8")
    assert f'"{fbc.STAGE_SAFE_FALLBACK}"' in source


def test_hooks_scoped_resolution_is_not_counted(monkeypatch):
    """`hooks.*` keys all map to the shared coordinator_core.hooks package, so
    step 1 is always a no-op and the hooks-scoped stage fires on 100% of
    hooks.* dispatches BY DESIGN. Counting it would put a record on every tool
    call and bury the ops-wide cliff this telemetry exists to surface —
    measured firing on every hooks.* op before this was removed."""
    calls = []
    monkeypatch.setattr(ipc, "_record_registry_fallback", lambda *a, **k: calls.append(a))

    handler = ipc.get_op_handler("hooks.track_touched_files")

    assert handler is not None, "precondition: the hooks op resolves"
    assert calls == [], f"hooks-scoped resolution must not count, got {calls}"


def test_fast_path_records_nothing(monkeypatch):
    """A resolution served by OP_MODULE_MAP's targeted import must not count.
    If it did, the counter would fire on every ordinary dispatch and the
    cliff it exists to surface would be invisible in the noise."""
    calls = []
    monkeypatch.setattr(ipc, "_record_registry_fallback", lambda *a, **k: calls.append(a))

    handler = ipc._lazy_import_and_lookup("schema.describe")

    assert handler is not None, "precondition: schema.describe resolves via the map"
    assert calls == []


def test_unmapped_op_records_the_safe_fallback_stage(monkeypatch):
    calls = []
    monkeypatch.setattr(ipc, "_record_registry_fallback", lambda *a, **k: calls.append((a, k)))

    ipc._lazy_import_and_lookup("definitely.not.a.real.op")

    assert calls, "an unmapped op must count as a fallback"
    (method, stage), kwargs = calls[-1]
    assert method == "definitely.not.a.real.op"
    assert stage == fbc.STAGE_SAFE_FALLBACK
    assert kwargs["mapped"] is False


def test_telemetry_failure_never_breaks_dispatch(monkeypatch):
    """The whole point of the swallow in ipc._record_registry_fallback. An op
    that would otherwise resolve must still resolve when the counter raises."""
    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(fbc, "record_registry_fallback", _boom)

    # exercised through the real helper, not a stub, so the try/except is live
    ipc._record_registry_fallback("x.y", fbc.STAGE_SAFE_FALLBACK, mapped=False)

    handler = ipc._lazy_import_and_lookup("schema.describe")
    assert handler is not None

def test_unmapped_op_lands_a_real_record_on_disk_via_caller_cwd(tmp_path, monkeypatch):
    """End-to-end through the REAL sink: `_lazy_import_and_lookup` must thread
    a caller cwd it already has in hand into `_record_registry_fallback` ->
    `record_registry_fallback`, whose own `cwd=None` default falls straight
    through `resolve_git_root_cheap(None)`'s first guard and silently skips
    the write. A mocked sink would reproduce exactly that blindness.
    """
    monkeypatch.setattr(fbc, "resolve_git_root_cheap", lambda cwd=None: cwd)
    monkeypatch.setattr(
        "coordinator_core.ops.session_context.resolve_current_session_id",
        lambda: "sid-real",
    )

    msg = {ipc._CALLER_CWD_FIELD: str(tmp_path)}
    handler = ipc._lazy_import_and_lookup("definitely.not.a.real.op", msg)

    assert handler is None, "precondition: the op is genuinely unregistered"
    path = _counts_path(tmp_path, "sid-real")
    assert path.exists(), "record_registry_fallback must have written through to disk"
    record = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["op"] == "definitely.not.a.real.op"
    assert record["stage"] == fbc.STAGE_SAFE_FALLBACK
    assert record["mapped"] is False


def test_every_hooks_key_maps_to_the_shared_package_value():
    """The hooks-stage exclusion is only safe while every `hooks.*` key maps to
    the SAME shared package value, which makes step 1 a guaranteed no-op for
    them and step 2 their designed path.

    Nothing else enforces that. Point one future `hooks.*` entry at a per-module
    path and it starts being served by step 1 sometimes and step 2 other times,
    with step 2's genuine misses still uncounted — reintroducing, for a subset
    of ops, exactly the blind spot this counter exists to close. Raised as
    Finding 4 by the code-reviewer on the shared-dispatch-seam slice.
    """
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    hooks_values = {v for k, v in OP_MODULE_MAP.items() if k.startswith("hooks.")}
    assert hooks_values, "precondition: the map carries hooks.* entries"
    assert hooks_values == {"coordinator_core.hooks"}, (
        "every hooks.* key must map to the shared coordinator_core.hooks package; "
        f"found {sorted(hooks_values)}. A per-module value here silently un-counts "
        "that op's registry-fallback escalations — see this test's docstring."
    )
