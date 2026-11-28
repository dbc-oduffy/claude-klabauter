"""Pins ask_warp_run: the run-stage `warp_run` link, and the sizing-matched listing fallback."""

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_contract as c

SIZING = "state/sizings/2026-11/2026-11-21-thing.yaml"
ASK = "ask-20261121T100000-aaaaaa"


def _dir(root: Path, name: str, **files) -> Path:
    d = root / c.RECORD_DIR_ROOT / name
    d.mkdir(parents=True)
    for fname, body in files.items():
        (d / fname.replace("_json", ".json")).write_text(json.dumps(body), encoding="utf-8")
    return d


def test_run_stage_warp_run_wins(tmp_path: Path) -> None:
    d = _dir(tmp_path, ASK)
    c.write_run_stage_at(d, "minted", warp_run="warp-x-20261121T100100-bbbbbb", session_id="s")
    assert c.ask_warp_run(tmp_path, ASK) == "warp-x-20261121T100100-bbbbbb"


def test_listing_fallback_picks_newest_matching_run_not_older_or_other_sizing(tmp_path: Path) -> None:
    _dir(tmp_path, ASK, run_json={"sizing_rel": SIZING})
    older = "warp-2026-11-21-thing-20261121T090000-cccccc"
    mid = "warp-2026-11-21-thing-20261121T100500-dddddd"
    newest = "warp-2026-11-21-thing-20261121T110000-eeeeee"
    other = "warp-2026-11-21-thing-20261121T120000-ffffff"
    _dir(tmp_path, older, run_json={"sizing_rel": SIZING})
    _dir(tmp_path, mid, run_json={"sizing_rel": SIZING})
    _dir(tmp_path, newest, run_json={"sizing_rel": SIZING})
    _dir(tmp_path, other, run_json={"sizing_rel": "state/sizings/2026-11/2026-11-21-other.yaml"})
    assert c.ask_warp_run(tmp_path, ASK) == newest


def test_never_launched_ask_and_unsafe_ids_read_none(tmp_path: Path) -> None:
    _dir(tmp_path, ASK, run_json={"sizing_rel": SIZING})
    _dir(tmp_path, "warp-2026-11-21-thing-20261121T090000-cccccc", run_json={"sizing_rel": SIZING})
    assert c.ask_warp_run(tmp_path, ASK) is None
    assert c.ask_warp_run(tmp_path, "ask-absent") is None
    assert c.ask_warp_run(tmp_path, "../evil") is None
    assert c.ask_warp_run(tmp_path, "") is None


def test_ask_without_sizing_reads_none(tmp_path: Path) -> None:
    _dir(tmp_path, ASK)
    assert c.ask_warp_run(tmp_path, ASK) is None
