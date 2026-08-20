"""Guards on the engine-root env-var census (C20).

The census is C11's routing input and C18's axis input. Two ways it can rot
silently, and each gets a test:

1. A NEW locator-shaped call site lands and nobody dispositions it. The census
   still renders, still looks complete, and C18 consumes a set with an
   unclassified member — which is exactly the silent repointing the whole
   workstream exists to prevent.
2. A dispositioned file is MOVED or DELETED. Its verdict then applies to
   nothing, and the map reads as more reviewed than it is.

Negative spec: these must not become vacuously green. The classifier's own
output is the input, so a test that only asserts "the script runs" would pass
against an empty tree. Each test below asserts on a non-empty measured
population first.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN.parent.parent
_SCRIPT = _BIN / "classify-env-var-callers.py"


def _load():
    spec = importlib.util.spec_from_file_location("classify_env_var_callers", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(scope="module")
def table(mod):
    return mod.classify(_REPO_ROOT)


def test_census_is_not_vacuous(table):
    """The population is real, so the assertions below mean something."""
    assert table["raw_file_count"] > 100, (
        f"census found only {table['raw_file_count']} files — the scan is broken, "
        "and every other assertion in this module is vacuous against it"
    )
    assert table["buckets"]["a-dispatch"]["file_count"] > 0
    assert table["buckets"]["b-writer"]["file_count"] > 0


def test_every_bucket_member_is_counted_once(table):
    """Buckets partition the surface — no file in two, none dropped."""
    seen: dict[str, str] = {}
    for bucket_id, info in table["buckets"].items():
        for entry in info["files"]:
            prior = seen.get(entry["file"])
            assert prior is None, (
                f"{entry['file']} is in both {prior!r} and {bucket_id!r}; the bucketing "
                "is meant to be first-match-wins and disjoint"
            )
            seen[entry["file"]] = bucket_id
    assert len(seen) == table["raw_file_count"]


def test_no_undispositioned_locator_candidates(table):
    """A non-dispatch use site with no recorded verdict blocks the census.

    Fix by reviewing the file and adding a row to `_REVIEWED_DISPOSITIONS` in
    `coordinator/bin/classify-env-var-callers.py` — not by widening the
    dispatch heuristic until the bucket empties, which would route a
    source-checkout consumer to the engine accessor and fail silently.
    """
    unreviewed = [e["file"] for e in table["buckets"]["c-locator-review"]["files"]]
    assert not unreviewed, (
        "these call sites use the resolved engine root for something other than "
        "reaching the engine, and carry no recorded axis verdict:\n  "
        + "\n  ".join(unreviewed)
    )


def test_disposition_map_has_no_stale_entries(mod):
    """Every dispositioned path still exists, so no verdict applies to nothing."""
    assert mod._REVIEWED_DISPOSITIONS, "the disposition map is empty — nothing is reviewed"
    missing = [
        rel for rel in mod._REVIEWED_DISPOSITIONS if not (_REPO_ROOT / rel).exists()
    ]
    assert not missing, (
        "dispositioned files that no longer exist at that path — the verdict is stale "
        "and the file, if it moved, is now unreviewed:\n  " + "\n  ".join(missing)
    )


def test_disposition_verdicts_are_from_the_known_set(mod):
    """A typo'd verdict would raise a KeyError deep in classify(); catch it here."""
    allowed = {"dispatch", "locator", "ladder", "fixture"}
    bad = {
        rel: verdict
        for rel, (verdict, _reason) in mod._REVIEWED_DISPOSITIONS.items()
        if verdict not in allowed
    }
    assert not bad, f"unknown axis verdicts: {bad}"


def test_every_disposition_carries_a_reason(mod):
    """A verdict without a reason is an assertion, not a record."""
    thin = {
        rel: reason
        for rel, (_verdict, reason) in mod._REVIEWED_DISPOSITIONS.items()
        if len(reason.strip()) < 20
    }
    assert not thin, f"dispositions whose reason says nothing checkable: {thin}"


def test_locator_bucket_is_named_and_small(table):
    """Case (c) is rare by the plan's own prediction; a large bucket means the
    dispatch heuristic broke, not that the tree changed shape."""
    locator = [e["file"] for e in table["buckets"]["c-locator"]["files"]]
    assert locator, "no locator-axis consumers at all — the axis split has nothing to serve"
    assert len(locator) < 20, (
        f"{len(locator)} locator-axis consumers is far above the handful C11 predicts; "
        "re-check the dispatch use-site patterns before routing anything"
    )
