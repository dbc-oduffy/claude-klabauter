"""Pins the census-shaped payload: the cartography output validates; broken shapes each yield a finding."""
from __future__ import annotations

from coordinator_core.architecture_survey import census_shape


def _results():
    return [
        {
            "bucket_id": "core",
            "files": [
                {"path": "core/a.py", "loc": None, "oversized": False},
                {"path": "core/b.py", "loc": None, "oversized": True},
            ],
            "anomalies": [],
        }
    ]


def _payload(source="cartography"):
    return census_shape.build(
        _results(), source=source, chunk_table_path="t.json", counts={"bucketed_total": 2}, unbucketed=0
    )


def test_cartography_shaped_output_validates():
    payload = _payload()
    assert census_shape.validate(payload) == []
    assert payload["oversizedCount"] == 1
    assert payload["source"] == "cartography"
    assert set(payload) == set(census_shape.REQUIRED_KEYS)


def test_tree_source_validates():
    assert census_shape.validate(_payload("tree")) == []


def test_missing_key_is_a_finding():
    payload = _payload()
    del payload["unbucketedCount"]
    assert census_shape.validate(payload) == ["missing key: unbucketedCount"]


def test_non_posix_path_is_a_finding():
    for bad in ("core\\a.py", "/abs/a.py", "C" + ":/x/a.py"):
        payload = _payload()
        payload["censusShapedResults"][0]["files"][0]["path"] = bad
        findings = census_shape.validate(payload)
        assert len(findings) == 1 and "non-POSIX path" in findings[0]


def test_unknown_source_is_a_finding():
    findings = census_shape.validate(_payload("guess"))
    assert len(findings) == 1 and "unknown source" in findings[0]


def test_non_object_and_bad_bucket_shapes():
    assert census_shape.validate([]) == ["payload is not an object"]
    payload = _payload()
    payload["censusShapedResults"][0].pop("anomalies")
    assert census_shape.validate(payload) == ["censusShapedResults[0] missing key: anomalies"]
