"""
Tests for the provenance receipt ``dispatch.emit`` writes beside every
emitted script (``coordinator_core.ops.dispatch_emit.op``).

The receipt's shape is a cross-repo contract with DoE-claude's wrapper CLI
``coordinator/bin/emit-dispatch-workflow.py`` (the other producer) and a
DoE-side hook (the consumer, which verifies ``sha256`` against the script
bytes). These tests pin the keys, the digest, the best-effort failure mode,
and the fact that no session id is ever fabricated.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C5.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import (
    _dispatch_emit,
    emission_receipt_path,
)
from coordinator_core.ops.dispatch_emit.tests.test_op import _write_fixture_plan
from coordinator_core.session.core import SESSION_ENV_PRECEDENCE

_RECEIPT_KEYS = {"sha256", "session_id", "emitted_at", "plan"}


def _emit(tmp_path, **extra):
    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "emitted.mjs"
    params = {"plan_path": str(plan_path), "output_path": str(output_path)}
    params.update(extra)
    return _dispatch_emit(params), output_path, plan_path


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_receipt_is_written_beside_the_script_with_the_contract_keys(tmp_path):
    result, output_path, plan_path = _emit(tmp_path, session_id="sess-abc")

    receipt_path = emission_receipt_path(output_path.resolve())
    assert receipt_path.name == "emitted.mjs.emitted.json"
    assert receipt_path.parent == output_path.resolve().parent
    assert receipt_path.is_file()
    assert result["receipt"] == str(receipt_path)

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == _RECEIPT_KEYS
    assert receipt["plan"] == plan_path.name
    assert receipt["session_id"] == "sess-abc"
    # isoformat(timespec="seconds") -- no sub-second component, no offset.
    assert len(receipt["emitted_at"]) == len("2026-09-17T12:34:56")
    assert receipt["emitted_at"][10] == "T"


def test_receipt_is_serialised_the_way_the_other_producer_serialises_it(tmp_path):
    """Sorted keys, two-space indent, one trailing newline -- byte-identical to
    DoE's wrapper, so a diff between two routes' receipts is content-only."""
    _result, output_path, _plan_path = _emit(tmp_path)

    raw = emission_receipt_path(output_path.resolve()).read_text(encoding="utf-8")

    assert raw.endswith("}\n")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True) + "\n"


def test_receipt_sha256_matches_the_script_bytes_on_disk(tmp_path):
    import hashlib

    _result, output_path, _plan_path = _emit(tmp_path)

    receipt = json.loads(
        emission_receipt_path(output_path.resolve()).read_text(encoding="utf-8")
    )
    expected = hashlib.sha256(output_path.read_bytes()).hexdigest()

    assert receipt["sha256"] == expected


# ---------------------------------------------------------------------------
# Session id -- taken, never minted
# ---------------------------------------------------------------------------


def _clear_session_env(monkeypatch):
    for var in SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)


def _receipt_of(output_path):
    return json.loads(
        emission_receipt_path(output_path.resolve()).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("var", SESSION_ENV_PRECEDENCE)
def test_session_id_resolves_through_every_canonical_tier(tmp_path, monkeypatch, var):
    """Each tier of the fleet-canonical ladder, not just one.

    `CLAUDE_CODE_SESSION_ID` is the tier actually populated on a Claude Code
    host, and a `CLAUDE_SESSION_ID`-only read skipped it -- writing an empty
    session_id that the fire gate then refused.
    """
    _clear_session_env(monkeypatch)
    monkeypatch.setenv(var, f"sess-via-{var}")

    _result, output_path, _plan_path = _emit(tmp_path)

    assert _receipt_of(output_path)["session_id"] == f"sess-via-{var}"


def test_the_canonical_tiers_are_ordered_as_the_resolver_orders_them(
    tmp_path, monkeypatch
):
    _clear_session_env(monkeypatch)
    for var in SESSION_ENV_PRECEDENCE:
        monkeypatch.setenv(var, f"sess-via-{var}")

    _result, output_path, _plan_path = _emit(tmp_path)

    assert (
        _receipt_of(output_path)["session_id"]
        == f"sess-via-{SESSION_ENV_PRECEDENCE[0]}"
    )


def test_an_explicit_param_wins_over_the_environment(tmp_path, monkeypatch):
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-from-env")

    _result, output_path, _plan_path = _emit(tmp_path, session_id="sess-explicit")

    assert _receipt_of(output_path)["session_id"] == "sess-explicit"


def test_session_id_is_empty_and_never_fabricated_when_nothing_names_one(
    tmp_path, monkeypatch
):
    """A phantom id attributes an emission to a session that never ran -- the
    exact false attribution the receipt exists to prevent."""
    _clear_session_env(monkeypatch)

    _result, output_path, _plan_path = _emit(tmp_path)

    assert _receipt_of(output_path)["session_id"] == ""


# ---------------------------------------------------------------------------
# Best-effort: the script is the deliverable
# ---------------------------------------------------------------------------


def test_emit_succeeds_with_the_same_verdict_when_the_receipt_cannot_be_written(
    tmp_path, capsys
):
    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "emitted.mjs"
    # A DIRECTORY sitting exactly where the sidecar goes: write_text() raises,
    # and the emit must be unaffected.
    (tmp_path / "emitted.mjs.emitted.json").mkdir()

    result = _dispatch_emit(
        {"plan_path": str(plan_path), "output_path": str(output_path)}
    )

    assert result["ok"] is True
    assert result["error_count"] == 0
    assert result["path"] == str(output_path.resolve())
    assert output_path.is_file()
    assert output_path.read_text(encoding="utf-8")
    assert result["receipt"] is None
    assert "provenance receipt" in capsys.readouterr().err


def test_a_monkeypatched_receipt_failure_never_fails_the_emit(tmp_path, capsys):
    def _explode(script_path):
        raise OSError("simulated digest failure")

    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "emitted.mjs"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(op_module, "_script_sha256", _explode)
        result = _dispatch_emit(
            {"plan_path": str(plan_path), "output_path": str(output_path)}
        )

    assert result["ok"] is True
    assert result["receipt"] is None
    assert output_path.is_file()
    assert not emission_receipt_path(output_path.resolve()).exists()
    assert "simulated digest failure" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# force re-stamps
# ---------------------------------------------------------------------------


def test_force_overwrite_restamps_the_receipt_to_the_new_bytes(tmp_path):
    import hashlib

    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "emitted.mjs"
    output_path.write_text("// a peer's emission", encoding="utf-8", newline="")
    receipt_path = emission_receipt_path(output_path.resolve())
    receipt_path.write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(b"// a peer's emission").hexdigest(),
                "session_id": "sess-peer",
                "emitted_at": "2026-09-16T09:00:00",
                "plan": plan_path.name,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    stale_sha = json.loads(receipt_path.read_text(encoding="utf-8"))["sha256"]

    result = _dispatch_emit(
        {
            "plan_path": str(plan_path),
            "output_path": str(output_path),
            "force": True,
            "session_id": "sess-ours",
        }
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result["receipt"] == str(receipt_path)
    assert receipt["sha256"] != stale_sha
    assert receipt["sha256"] == hashlib.sha256(output_path.read_bytes()).hexdigest()
    assert receipt["session_id"] == "sess-ours"
    assert set(receipt) == _RECEIPT_KEYS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
