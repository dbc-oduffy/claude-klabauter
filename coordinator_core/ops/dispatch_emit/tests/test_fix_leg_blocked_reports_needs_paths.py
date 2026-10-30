"""A fix leg BLOCKED on a path outside the footprint reports it in needs_paths: the review halt-fix loop
carries it into the halt's `needs_paths`; the ledger fix leg's schema carries it too."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL

from .test_ask_compose_halt_self_correction import _OWN, _OWNED, _drive, _done, _red
from .test_ask_compose_ledger_recovery_loop import _NAMED, _record, _run, _script
from .test_ask_compose_stage_handle import stage_root  # noqa: F401

SCHEMA = "coordinator_core/frontmatter/schemas/sizing-object.schema.json"

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _blocked(needs, paths=()):
    return {"status": "BLOCKED", "paths": list(paths), "reason": "needs a path outside the footprint", "needs_paths": list(needs)}


def test_a_blocked_leg_naming_an_out_of_footprint_path_halts_with_it_in_needs_paths():
    out = _drive(reverify=_red(_OWNED), replies={HALT_FIX_LABEL: [_blocked([SCHEMA])]})

    sc = out["halted"]["self_correction"]
    assert sc["status"] == "scope"
    assert sc["needs_paths"] == [SCHEMA]
    assert [c["label"] for c in out["calls"]] == [HALT_FIX_LABEL]


def test_a_blocked_leg_naming_only_in_footprint_paths_does_not_become_scope():
    out = _drive(reverify=_red(_OWNED), replies={HALT_FIX_LABEL: [_blocked([_OWN])]})

    assert out["halted"]["self_correction"]["status"] != "scope"


def test_a_blocked_leg_with_empty_needs_paths_is_unchanged():
    out = _drive(reverify=_red(_OWNED), replies={HALT_FIX_LABEL: [_blocked([])]})

    assert out["halted"]["self_correction"]["needs_paths"] == []


def test_a_done_leg_is_unchanged():
    out = _drive(reverify=_red(_OWNED), replies={HALT_FIX_LABEL: [_done(_OWN, "tools/new_cli.py")]})

    assert out["halted"]["self_correction"]["needs_paths"] == ["tools/new_cli.py"]


def test_a_blocked_ledger_fix_records_out_of_footprint_needs_paths(monkeypatch):
    blocked = {"status": "BLOCKED", "paths": [], "reason": "needs a path outside the footprint",
               "needs_paths": [SCHEMA, "pkg/a.py"]}
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": blocked})

    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked"
    assert rec["paths"] == [SCHEMA]


def test_the_ledger_fix_schema_accepts_needs_paths():
    schema = ask_compose._LEDGER_FIX_SCHEMA_NEEDS
    assert "needs_paths" in schema["properties"] and "needs_paths" in schema["required"]
    assert schema["additionalProperties"] is False
