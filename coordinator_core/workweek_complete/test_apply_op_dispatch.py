"""
coordinator_core.workweek_complete.test_apply_op_dispatch — AC4/AC7
coverage for C7's admission control on `workweek_complete.apply`.

Purpose: `workweek_complete.apply` is one of the three completion-family
tables (plan § The discriminator for the mixed end state) — its UNIT does
not change (still `cli`-named, still in-process `CONSUMES_MANIFEST`
dispatch, never a registered op). What C7 adds is the CONTROL: `_resolve_cli`
now additionally calls the shared `apply_base.assert_dispatchable` before
resolving, so this table is no longer left with review-only admission.

AC4: an unrecognized/un-allowlisted verb fails loud (never degrades to
inert) before its own directive dispatches.

Spec backlink: docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md § C7

No process spawn, no git — fast tier.
"""

from __future__ import annotations

import types

import pytest

import coordinator_core.contract.apply_base as apply_base
from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.ceremony_common.apply_halt import UnrecognizedDirective
from coordinator_core.contract.apply_base import UnrecognizedDirective as ApplyBaseUnrecognizedDirective
from coordinator_core.workweek_complete import apply as wwc_apply


def test_every_consumes_manifest_member_is_allowlisted() -> None:
    """The completion-family entry must cover the whole manifest — no
    dispatchable verb is left un-admitted."""
    allowed = ASSEMBLER_DISPATCHABLE.get("workweek_complete", frozenset())
    for cli_name in wwc_apply._CLI_DISPATCH:
        assert cli_name in allowed, (
            f"{cli_name!r} is in workweek_complete's CONSUMES_MANIFEST-derived "
            f"_CLI_DISPATCH but absent from ASSEMBLER_DISPATCHABLE['workweek_complete']"
        )


def test_resolve_cli_happy_path_still_resolves() -> None:
    path = wwc_apply._resolve_cli("query-records")
    assert path.exists() or path.name in ("query-records.py", "query-records")


def test_resolve_cli_unrecognized_name_raises_before_dispatch() -> None:
    with pytest.raises(UnrecognizedDirective):
        wwc_apply._resolve_cli("not-a-real-cli-name")


def test_resolve_cli_reaches_nothing_when_control_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8's shape, applied here: a name genuinely in CONSUMES_MANIFEST but
    stripped from ASSEMBLER_DISPATCHABLE must still be refused — the
    membership check in `_CLI_DISPATCH` alone must not be sufficient."""
    monkeypatch.setattr(apply_base, "ASSEMBLER_DISPATCHABLE", types.MappingProxyType({}))
    with pytest.raises(ApplyBaseUnrecognizedDirective, match="dispatchable set"):
        wwc_apply._resolve_cli("query-records")


def test_load_cli_module_denial_reaches_no_module_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the pre-validation property one level up: `_load_cli_module`
    (the actual dispatch entry point `_dispatch_directive` calls) refuses
    via `_resolve_cli`'s admission check before ever caching or executing
    a module — no partial dispatch side effect."""
    monkeypatch.setattr(apply_base, "ASSEMBLER_DISPATCHABLE", types.MappingProxyType({}))
    with pytest.raises(ApplyBaseUnrecognizedDirective):
        wwc_apply._load_cli_module("query-records")
    assert "query-records" not in wwc_apply._LOADED_MODULES


def test_admission_pre_pass_refuses_whole_run_before_any_directive_dispatches(monkeypatch) -> None:
    """F1 (cold review 2026-08-19): an un-admitted `cli`, sequenced SECOND
    in a two-directive list, must fail the WHOLE run before any directive
    dispatches — including the FIRST, individually-resolvable directive.
    The un-admitted entry sitting second is the point: a first-position
    variant would prove nothing about whole-run pre-validation, since a
    per-directive halt would already refuse it."""
    directives = [
        {"id": "d1", "cli": "query-records", "args": []},
        {"id": "d2", "cli": "not-a-real-cli-name", "args": []},
    ]
    def _never_loads(cli_name: str):
        raise AssertionError(
            f"pre-pass regressed: {cli_name!r} reached the dispatch loop, which "
            "would import and run the real coordinator/bin script in-process"
        )

    monkeypatch.setattr(wwc_apply, "_load_cli_module", _never_loads)
    exit_code, report = wwc_apply._execute_directives(directives, [], {})
    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.DIRECTIVE_FAILED)
    assert report["landed"] == []
    assert report["blocked"] == []
    assert report["failed"] and report["failed"][0]["id"] is None


def test_admission_pre_pass_skips_an_already_satisfied_directive(monkeypatch) -> None:
    """Slice-B review finding 1 (2026-08-20): an `already_satisfied` directive
    cannot dispatch, so the pre-pass must not refuse the whole run over its
    verb. A gate-blocked directive is deliberately still checked."""

    def _never_loads(cli_name: str):
        raise AssertionError(f"{cli_name!r} must not dispatch in this test")

    monkeypatch.setattr(wwc_apply, "_load_cli_module", _never_loads)
    directives = [
        {"id": "d1", "cli": "a-verb-that-left-the-allowlist", "args": [],
         "already_satisfied": True},
    ]
    exit_code, report = wwc_apply._execute_directives(directives, [], {})

    assert exit_code != int(wwc_apply.WorkweekApplyExitCode.DIRECTIVE_FAILED)
    assert report["landed"] == ["d1"]
