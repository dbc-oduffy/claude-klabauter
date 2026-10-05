"""Oracle: the census equals the declared subset of the retired live scanner's records.

`fixtures/census_oracle.json` holds the 237 declaration-derived records (every record
whose verdict is not WRITE_TARGET_UNRESOLVED) of the scanner's former oracle, in the same
JSON-stable serialisation: sorted by generator, `Verdict` by `.value`, tuples as lists.

The census runs on the live tree through `assemble` with an in-process candidate finder
and a tmp cache dir, so it spawns nothing and never reads a developer's machine cache.

Regeneration: rerun the census, serialise with `serialize_records`, review the diff.
A census edit that changes a declared record is a defect, never a recapture; only a
tracked-tree change to a module's own declaration moves this fixture.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from coordinator_core.ops.staleness_git import Verdict

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "census_oracle.json"

_COLUMN0_DECLARATION = re.compile(
    rb"^(GENERATES|MUTATES|MUTATES_APPEND|GENERATES_EXTERNAL|UNSTAMPED_BY_DESIGN)\s*(:[^=]*)?=",
    re.MULTILINE,
)


def _serialize_value(value: object) -> object:
    """Render a record into a JSON-stable shape; raise on any shape without a rule."""
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
    raise TypeError(f"census oracle serializer has no rule for {type(value)!r}: {value!r}")


def serialize_records(records: list) -> str:
    """Stable JSON text, sorted by generator, matching the fixture's layout."""
    ordered = sorted(records, key=lambda record: record.generator)
    return json.dumps([_serialize_value(record) for record in ordered], indent=2, sort_keys=True)


def _in_process_finder(repo_root: Path, missing: list[str]) -> list[str]:
    """Candidates among `missing`: modules with a column-0 declaration token."""
    found = []
    for rel in missing:
        try:
            data = (repo_root / rel).read_bytes()
        except OSError:
            continue
        if _COLUMN0_DECLARATION.search(data):
            found.append(rel)
    return found


@pytest.mark.cadence
def test_census_equals_declared_oracle(tmp_path: Path) -> None:
    """Field-for-field equality of the census with the declared-record fixture; names the first diverging generator."""
    from coordinator_core.ops.generator_census.assemble import assemble

    census_text = serialize_records(assemble(REPO_ROOT, cold=_in_process_finder, cache_dir=tmp_path))
    fixture_text = FIXTURE_PATH.read_text(encoding="utf-8").rstrip("\r\n")

    assert census_text != "[]", "the census returned no records against the live repo"

    if census_text != fixture_text:
        census_by_path = {entry["generator"]: entry for entry in json.loads(census_text)}
        fixture_by_path = {entry["generator"]: entry for entry in json.loads(fixture_text)}
        for rel_path in sorted(set(census_by_path) | set(fixture_by_path)):
            if census_by_path.get(rel_path) != fixture_by_path.get(rel_path):
                pytest.fail(
                    f"census diverged from the oracle at generator={rel_path!r}:\n"
                    f"  census:  {census_by_path.get(rel_path)!r}\n"
                    f"  fixture: {fixture_by_path.get(rel_path)!r}"
                )
        pytest.fail("census diverged from the oracle (no single generator differed)")


def test_fixture_is_the_declared_subset_shape() -> None:
    """The fixture holds only declared records, sorted by generator, with no write-detector verdict."""
    records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    generators = [record["generator"] for record in records]
    assert generators == sorted(generators)
    assert len(set(generators)) == len(generators)
    assert Verdict.WRITE_TARGET_UNRESOLVED.value not in {record["verdict"] for record in records}
    assert all(set(record) == {"detail", "generator", "mutates", "pairs", "verdict"} for record in records)
