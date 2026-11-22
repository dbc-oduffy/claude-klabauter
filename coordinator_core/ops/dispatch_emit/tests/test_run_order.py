"""Pure tests for run_order: validate, record, read_for_sizing."""

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import credit_gate
from coordinator_core.ops.dispatch_emit import run_order as ro

SIZING = "state/sizings/2026-11/2026-11-21-aa0e33-2-x.yaml"


def test_validate_dedupes_in_order() -> None:
    assert ro.validate(["C4", "C1", "C4", "a.b-c_1"]) == ["C4", "C1", "a.b-c_1"]


@pytest.mark.parametrize(
    "bad",
    [
        "C1",
        {"a": 1},
        [""],
        ["-C1"],
        ["C 1"],
        [3],
        [sorted(credit_gate.BOOKKEEPING_CHUNK_IDS)[0]],
        [f"C{i}" for i in range(ro.MAX_ENTRIES + 1)],
    ],
)
def test_validate_refuses(bad) -> None:
    with pytest.raises(ValueError):
        ro.validate(bad)


def test_validate_names_first_bad_entry() -> None:
    with pytest.raises(ValueError, match=r"order_rows\[1\]"):
        ro.validate(["C1", ""])


def test_record_round_trip(tmp_path: Path) -> None:
    d = tmp_path / "run"
    sink = ro.record(d, ["C4", "C1"], "2026-01-01T00:00:00Z")
    assert sink == d / ro.ORDER_FILE
    assert json.loads(sink.read_text(encoding="utf-8").splitlines()[0]) == {
        "order": ["C4", "C1"],
        "at": "2026-01-01T00:00:00Z",
    }
    assert ro._last_order(d) == ["C4", "C1"]


def _run(root: Path, name: str, sizing: str | None) -> Path:
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    if sizing is not None:
        (d / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    return d


def test_read_for_sizing_newest_wins_and_empty_clears(tmp_path: Path) -> None:
    old = _run(tmp_path, "ask-20261121T100000-aaaaaa", SIZING)
    new = _run(tmp_path, "ask-20261121T120000-bbbbbb", SIZING)
    own = _run(tmp_path, "ask-20261121T130000-cccccc", SIZING)
    ro.record(old, ["C1"], "t")
    ro.record(new, ["C4", "C2"], "t")
    assert ro.read_for_sizing(tmp_path, own.name, SIZING) == ["C4", "C2"]
    ro.record(own, [], "t")
    assert ro.read_for_sizing(tmp_path, own.name, SIZING) == []


def test_read_for_sizing_ignores_other_sizing_and_missing(tmp_path: Path) -> None:
    other = _run(tmp_path, "ask-20261121T120000-bbbbbb", "state/sizings/2026-11/2026-11-21-zz-other.yaml")
    own = _run(tmp_path, "ask-20261121T130000-cccccc", SIZING)
    ro.record(other, ["C9"], "t")
    assert ro.read_for_sizing(tmp_path, own.name, SIZING) == []
    assert ro.read_for_sizing(tmp_path / "nowhere", "ask-20261121T130000-cccccc", SIZING) == []


def test_read_for_sizing_skips_corrupt_lines(tmp_path: Path) -> None:
    own = _run(tmp_path, "ask-20261121T130000-cccccc", SIZING)
    ro.record(own, ["C2"], "t")
    with (own / ro.ORDER_FILE).open("a", encoding="utf-8") as f:
        f.write("not json\n{\"order\": \"bad\"}\n")
    assert ro.read_for_sizing(tmp_path, own.name, SIZING) == ["C2"]
