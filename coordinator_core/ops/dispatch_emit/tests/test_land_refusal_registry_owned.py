"""Pins that no land-refusal engine_bug entry is unowned and every queue-row owner names a row
in the bug-queue file."""

import inspect
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import land_refusal_registry as reg

QUEUE_FILE = (
    Path(__file__).resolve().parents[4] / "state/bug-queue/2026-11-18-land-refusal-engine-bug-owners.yaml"
)
ROWS = {row["id"]: row for row in yaml.safe_load(QUEUE_FILE.read_text(encoding="utf-8"))["rows"]}
PREFIX = "claude-klabauter:"
BUGS = sorted(reg.engine_bugs().items())


def test_no_engine_bug_entry_is_unowned():
    assert [k for k, v in BUGS if v.owner is None] == []


def test_bug_factory_requires_an_owner():
    assert inspect.signature(reg._bug).parameters["owner"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        reg._bug("refusal", "m::f")  # type: ignore[call-arg]


def test_queue_rows_constant_equals_the_bug_queue_file():
    assert list(reg.QUEUE_ROWS) == list(ROWS)
    assert reg.OWNERS == reg.SIBLING_OWNERS | {PREFIX + r for r in ROWS}


@pytest.mark.parametrize("key,entry", BUGS)
def test_every_engine_bug_owner_is_a_sibling_ask_or_a_queue_row(key, entry):
    assert entry.owner in reg.OWNERS, key
    assert "44a104" not in entry.owner, key
    if entry.owner in reg.SIBLING_OWNERS:
        return
    assert entry.owner.startswith(PREFIX), key
    row = ROWS[entry.owner[len(PREFIX):]]
    assert row["class"] == key
    assert row["emitted_by"] == entry.emitted_by
    assert row["fix"].strip()


def test_every_queue_row_is_owned_by_a_live_engine_bug():
    owners = {v.owner for _, v in BUGS}
    for row_id, row in ROWS.items():
        assert PREFIX + row_id in owners, row_id
        assert reg.LAND_REFUSAL_REGISTRY[row["class"]].engine_bug, row_id


def test_a_harm_justified_entry_is_never_owned_as_a_bug():
    for key, entry in reg.LAND_REFUSAL_REGISTRY.items():
        if entry.harm is not None:
            assert entry.owner is None, key
