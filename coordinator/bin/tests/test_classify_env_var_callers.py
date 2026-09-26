
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
    assert table["raw_file_count"] > 100, (
        f"census found only {table['raw_file_count']} files — the scan is broken, "
        "and every other assertion in this module is vacuous against it"
    )
    assert table["buckets"]["a-dispatch"]["file_count"] > 0
    assert table["buckets"]["b-writer"]["file_count"] > 0


def test_every_bucket_member_is_counted_once(table):
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
    assert mod._REVIEWED_DISPOSITIONS, "the disposition map is empty — nothing is reviewed"
    missing = [
        rel for rel in mod._REVIEWED_DISPOSITIONS if not (_REPO_ROOT / rel).exists()
    ]
    assert not missing, (
        "dispositioned files that no longer exist at that path — the verdict is stale "
        "and the file, if it moved, is now unreviewed:\n  " + "\n  ".join(missing)
    )


def test_disposition_verdicts_are_from_the_known_set(mod):
    allowed = {"dispatch", "locator", "ladder", "fixture"}
    bad = {
        rel: verdict
        for rel, (verdict, _reason) in mod._REVIEWED_DISPOSITIONS.items()
        if verdict not in allowed
    }
    assert not bad, f"unknown axis verdicts: {bad}"


def test_every_disposition_carries_a_reason(mod):
    thin = {
        rel: reason
        for rel, (_verdict, reason) in mod._REVIEWED_DISPOSITIONS.items()
        if len(reason.strip()) < 20
    }
    assert not thin, f"dispositions whose reason says nothing checkable: {thin}"


def test_locator_bucket_is_named_and_small(table):
    locator = [e["file"] for e in table["buckets"]["c-locator"]["files"]]
    assert locator, "no locator-axis consumers at all — the axis split has nothing to serve"
    assert len(locator) < 20, (
        f"{len(locator)} locator-axis consumers is far above the handful C11 predicts; "
        "re-check the dispatch use-site patterns before routing anything"
    )
