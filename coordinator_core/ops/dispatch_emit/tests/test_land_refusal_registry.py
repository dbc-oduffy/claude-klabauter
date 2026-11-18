"""Pins the closed land-refusal registry: every entry is harm-justified or an engine bug, and
its emitter names a real function."""

import ast
import importlib.util

import pytest

from coordinator_core import deny_registry as dr
from coordinator_core.ops.dispatch_emit import land_refusal_registry as reg
from coordinator_core.ops.dispatch_emit import landing_refusal_kinds as k

ENTRIES = sorted(reg.LAND_REFUSAL_REGISTRY.items())


def test_registry_is_frozen():
    with pytest.raises(TypeError):
        reg.LAND_REFUSAL_REGISTRY["x"] = reg.RefusalClass("refusal", None, True, None, "m::f")  # type: ignore[index]
    with pytest.raises(Exception):
        reg.LAND_REFUSAL_REGISTRY["head-moved"].harm = None  # type: ignore[misc]


@pytest.mark.parametrize("key,entry", ENTRIES)
def test_every_denying_entry_carries_harm_or_engine_bug(key, entry):
    assert entry.kind in reg.KINDS
    if entry.denies:
        assert entry.harm or entry.engine_bug, key
    if entry.harm is not None:
        assert entry.harm in dr.IRREVERSIBLE_CLASSES, key
        assert entry.engine_bug is False, f"{key}: a harm-justified reason is not an engine bug"
    assert entry.owner is None or entry.owner in reg.OWNERS, key
    assert entry.owner is None or entry.engine_bug or not entry.denies, key


@pytest.mark.parametrize("key,entry", ENTRIES)
def test_emitted_by_names_a_real_function(key, entry):
    module, _, function = entry.emitted_by.partition("::")
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin, key
    with open(spec.origin, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert function in names, f"{key}: {entry.emitted_by}"


def test_closed_table_reasons_are_harm_justified_refusals():
    for reason in k.LANDING_REFUSAL_KINDS:
        entry = reg.LAND_REFUSAL_REGISTRY[reason]
        assert entry.kind == "refusal" and entry.denies and entry.harm in dr.IRREVERSIBLE_CLASSES
        assert not entry.engine_bug
    for reason, cls in dr.LANDING_REFUSAL_CLASS.items():
        assert reg.LAND_REFUSAL_REGISTRY[reason].harm == cls


def test_provenance_kinds_are_imported_not_copied():
    assert k.PROVENANCE_KINDS <= set(reg.LAND_REFUSAL_REGISTRY)
    assert reg.PROVENANCE_ONLY
    for reason in reg.PROVENANCE_ONLY:
        entry = reg.LAND_REFUSAL_REGISTRY[reason]
        assert entry.denies is False and not entry.engine_bug and entry.harm is None


def test_every_hand_land_class_is_registered_with_its_owner():
    assert set(reg.HAND_LAND_CLASSES.values()) <= set(reg.LAND_REFUSAL_REGISTRY)
    owners = {key: reg.LAND_REFUSAL_REGISTRY[key].owner for key in reg.HAND_LAND_CLASSES.values()}
    assert owners["missing-target"] == "3c236b-1"
    assert owners["tests-not-run"] == "f9b322"
    assert owners["peer-claim-indeterminate"] == "17193e"
    assert owners["evicted-parked"] == "17193e"
    assert owners["land-deadline"] == "d0fa7d"
    assert owners["since-base-overlap"] == "since-base-overlap"
    assert reg.LAND_REFUSAL_REGISTRY["verdict-pin-stale"].engine_bug is True


def test_withholds_and_parks_are_registered():
    kinds = {e.kind for e in reg.LAND_REFUSAL_REGISTRY.values()}
    assert kinds == reg.KINDS
    assert reg.LAND_REFUSAL_REGISTRY["land-deadline"].kind == "park"
    assert reg.LAND_REFUSAL_REGISTRY["peer-claim-indeterminate"].kind == "withhold"


def test_classify_and_engine_bugs():
    assert reg.classify("not-a-reason") is None
    assert reg.classify("secret-in-diff").harm == dr.IRREVERSIBLE_EXTERNAL
    bugs = reg.engine_bugs()
    assert "verdict-pin-stale" in bugs and "secret-in-diff" not in bugs
