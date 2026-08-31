"""
Oracle regression for `coordinator_core.ops.generator_provenance.discover_generators`.

Purpose: C1 in `docs/plans/2026-08-31-generator-discovery-on-a-scan-cache.md` captures
a byte-identical snapshot of what `discover_generators(REPO_ROOT)` returns against
today's unchanged implementation. Later chunks restructure that function into a
scan-cache-backed shape; this fixture is the only artifact that proves the
restructure did not change discovery's answers. A full-string comparison is
deliberate: a restructure that returns the same record COUNT with a different
BASIS or verdict for some module is exactly the silent regression this guards
against, and a count-only or length-only check would not catch it.

Observed record count at capture time: 255.

Negative-spec:
  - This module does not test discovery's correctness (that is
    `test_generator_provenance.py`'s job) -- only that today's output is what
    tomorrow's refactor reproduces.
  - This module does not construct synthetic fixture modules; it sweeps the real
    repo, exactly as `discover_generators` does in production.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from coordinator_core.ops.generator_provenance import discover_generators
from coordinator_core.ops.staleness_git import Verdict

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "generator_discovery_oracle.json"


def _serialize_value(value: object) -> object:
    """Render a `discover_generators` return value into a JSON-stable shape.

    Recurses through dataclasses (`GeneratorRecord`, `Pair`) field-by-field so
    no field is silently dropped; converts any set/frozenset to a sorted list
    so interpreter iteration order cannot leak into the fixture; renders a
    `Verdict` enum member by its `.value`. Raises on any value shape not
    explicitly handled here rather than passing it through unexamined --
    a field this serializer cannot render is exactly the kind of silent gap
    the oracle exists to prevent.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _serialize_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Verdict):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(_serialize_value(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_serialize_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"generator_discovery_oracle serializer has no rule for {type(value)!r}: {value!r}")


def serialize_generator_records(records: list) -> str:
    """Stable, sorted-by-`rel_path` JSON text for a `discover_generators` result.

    Exported for reuse by C4's tests, which import this rather than
    reimplementing the serialization rules above.
    """
    ordered = sorted(records, key=lambda record: record.generator)
    return json.dumps([_serialize_value(record) for record in ordered], indent=2, sort_keys=True)


@pytest.mark.cadence
def test_discover_generators_matches_oracle() -> None:
    live = discover_generators(REPO_ROOT)
    live_text = serialize_generator_records(live)
    fixture_text = FIXTURE_PATH.read_text(encoding="utf-8")

    assert live_text != "[]", "discover_generators returned no records against the live repo"

    if live_text != fixture_text:
        live_data = json.loads(live_text)
        fixture_data = json.loads(fixture_text)
        live_by_path = {entry["generator"]: entry for entry in live_data}
        fixture_by_path = {entry["generator"]: entry for entry in fixture_data}

        for rel_path in sorted(set(live_by_path) | set(fixture_by_path)):
            if live_by_path.get(rel_path) != fixture_by_path.get(rel_path):
                pytest.fail(
                    f"discover_generators output diverged from the oracle fixture "
                    f"at rel_path={rel_path!r}:\n"
                    f"  live:    {live_by_path.get(rel_path)!r}\n"
                    f"  fixture: {fixture_by_path.get(rel_path)!r}"
                )

        pytest.fail("discover_generators output diverged from the oracle fixture (no single rel_path differed)")
