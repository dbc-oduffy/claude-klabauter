"""
test_coordinator_queue_append_yaml_gate.py — fail-loud round-trip gate regression (C2).

Spec backlink: pln-queue-append-quoter-gap-and-th-20af07 § C2

Covers:
  AC3 — `_build_yaml` (both copies) `yaml.safe_load`s the composed document
        before returning and raises on failure. A field value engineered to
        break composition (by monkeypatching `_yaml_quote_string` to return
        an intentionally UNQUOTED value that needs quoting — the widened
        quoter from C1 would otherwise catch every known-bad case, so this is
        how the gate's OWN defense-in-depth is exercised independent of C1)
        raises, and the message names the offending field.
  AC4 — the gate raises an exception; the test asserts on the exception type
        (ValueError), not a captured log line.
  AC7 — an ORDINARY write (composition succeeds) still emits byte-identical
        output to pre-gate `_build_yaml` — the gate is parse-to-check only,
        never a re-serialization.

Both live implementations are loaded and called DIRECTLY, same pattern as
test_coordinator_queue_append_quote_widening.py (sibling C1 test, same
loader helpers duplicated here as this file's own module-private copies —
kept intentionally un-shared per this repo's "no new shared test-infra
module for a two-file duplication" convention already used by the C1 file).

Run with: python3 -m pytest coordinator/bin/test_coordinator_queue_append_yaml_gate.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

import pytest
import yaml


def _repo_bin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(_repo_bin_dir()))


def _legacy_cli_path() -> str:
    return os.path.join(_repo_bin_dir(), "coordinator-queue-append.py")


def _load_legacy_cli_module():
    path = _legacy_cli_path()
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_queue_append_cli_yaml_gate_probe", path
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_native_op_module():
    repo_root = _repo_root()
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import coordinator_core.ops.queue_append as native_module

    return native_module


_LEGACY = _load_legacy_cli_module()
_NATIVE = _load_native_op_module()

_DEBT_BACKLOG_FIELDS: dict = {
    "created": "2026-08-11",
    "title": "Broken: value with colon-space, unquoted",
    "body": "Body text for the yaml-gate test.",
    "status": "open",
    "source": "daily-review/yaml-gate-test",
    "risk": "Risk text.",
    "proposed_action": "Proposed action text.",
    "system": {"linked_sessions": [], "provenance_completeness": "unknown"},
}


def _identity_quote(value: str) -> str:
    return value


def test_build_yaml_gate_raises_and_names_field_native(monkeypatch) -> None:
    monkeypatch.setattr(_NATIVE, "_yaml_quote_string", _identity_quote)
    with pytest.raises(ValueError) as excinfo:
        _NATIVE._build_yaml("debt-backlog", dict(_DEBT_BACKLOG_FIELDS))
    if "title" not in str(excinfo.value):
        raise AssertionError(
            f"native op: raised ValueError does not name the offending field "
            f"'title': {excinfo.value}"
        )


def test_build_yaml_gate_raises_and_names_field_legacy_cli(monkeypatch) -> None:
    monkeypatch.setattr(_LEGACY, "_yaml_quote_string", _identity_quote)
    with pytest.raises(ValueError) as excinfo:
        _LEGACY._build_yaml("debt-backlog", dict(_DEBT_BACKLOG_FIELDS))
    if "title" not in str(excinfo.value):
        raise AssertionError(
            f"legacy CLI: raised ValueError does not name the offending field "
            f"'title': {excinfo.value}"
        )


def test_gate_does_not_silently_return_malformed_document(monkeypatch) -> None:
    for name, module in (("native op", _NATIVE), ("legacy CLI", _LEGACY)):
        monkeypatch.setattr(module, "_yaml_quote_string", _identity_quote)
        try:
            result = module._build_yaml("debt-backlog", dict(_DEBT_BACKLOG_FIELDS))
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"{name}: _build_yaml returned {result!r} instead of raising "
                f"ValueError for a document engineered to fail yaml.safe_load"
            )
        monkeypatch.undo()


def test_ordinary_write_still_parses_and_is_unchanged_by_the_gate() -> None:
    fields = dict(_DEBT_BACKLOG_FIELDS)
    fields["title"] = "A well-formed title with no special characters"

    native_doc = _NATIVE._build_yaml("debt-backlog", dict(fields))
    legacy_doc = _LEGACY._build_yaml("debt-backlog", dict(fields))

    parsed = yaml.safe_load(native_doc)
    if parsed["title"] != fields["title"]:
        raise AssertionError(
            f"native op: title did not round-trip: {parsed['title']!r} != {fields['title']!r}"
        )
    if native_doc != _NATIVE._build_yaml("debt-backlog", dict(fields)):
        raise AssertionError("native op: _build_yaml is not idempotent on a well-formed input")
    if legacy_doc != _LEGACY._build_yaml("debt-backlog", dict(fields)):
        raise AssertionError("legacy CLI: _build_yaml is not idempotent on a well-formed input")

    if native_doc != legacy_doc:
        raise AssertionError(
            f"native op and legacy CLI _build_yaml output diverged for an "
            f"ordinary write:\nnative={native_doc!r}\nlegacy={legacy_doc!r}"
        )
