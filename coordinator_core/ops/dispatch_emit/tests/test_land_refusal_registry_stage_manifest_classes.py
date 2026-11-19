"""Pins the registry rows for the 44a104 false refusal: dependents_unstarted park, then
'no folded stage manifest'."""

from coordinator_core.ops.dispatch_emit import land_refusal_registry as reg
from coordinator_core.ops.dispatch_emit.ask_compose import NO_MANIFEST_PREFIX
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_DEPENDENTS_UNSTARTED
from coordinator_core.ops.dispatch_emit.halt_remedy_class import HALT_REMEDY, MECHANICAL

_PARK = "claude-klabauter:land-refusal-dependents-unstarted"
_MANIFEST = "claude-klabauter:land-refusal-no-folded-stage-manifest"


def test_dependents_unstarted_is_a_registered_park_engine_bug_with_a_queue_row_owner():
    entry = reg.classify(HALT_DEPENDENTS_UNSTARTED)
    assert entry is not None
    assert (entry.kind, entry.engine_bug, entry.harm, entry.owner) == ("park", True, None, _PARK)
    assert entry.emitted_by.endswith("ask_contract::dependents_unstarted_refire_next_action")


def test_no_folded_stage_manifest_is_a_registered_refusal_engine_bug_with_a_queue_row_owner():
    key = NO_MANIFEST_PREFIX.replace(" ", "-")
    entry = reg.classify(key)
    assert entry is not None
    assert (entry.kind, entry.engine_bug, entry.harm, entry.owner) == ("refusal", True, None, _MANIFEST)
    assert entry.emitted_by.endswith("ask_compose::compose_ask_script")


def test_both_owners_are_declared_and_neither_is_the_reporting_run():
    assert {_PARK, _MANIFEST} <= reg.OWNERS
    for key in (HALT_DEPENDENTS_UNSTARTED, "no-folded-stage-manifest"):
        assert "44a104" not in (reg.LAND_REFUSAL_REGISTRY[key].owner or "")


def test_both_classes_are_hand_land_classes_and_the_park_halt_is_mechanical():
    assert reg.HAND_LAND_CLASSES["dependents-unstarted-park"] == HALT_DEPENDENTS_UNSTARTED
    assert reg.HAND_LAND_CLASSES["no-folded-stage-manifest"] == "no-folded-stage-manifest"
    assert HALT_REMEDY[HALT_DEPENDENTS_UNSTARTED] == (MECHANICAL, "halt-record-carries-refire-next-action")
