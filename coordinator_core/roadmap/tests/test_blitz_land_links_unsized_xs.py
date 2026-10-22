"""
coordinator_core/roadmap/tests/test_blitz_land_links_unsized_xs.py

Subject: `blitz_land.land_wave`'s unsized-XS leg (`_link_unsized_xs`).

Claim: an XS dispatch baton in `routedElsewhere` with no sizing_object is paired with its
existing sizing on both edges and reported in `xsLinked`; an ambiguous match leaves both
files byte-identical and lands in `xsUnlinked`; non-XS entries are untouched; a batch of
two costs one `run_git` call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap import sizing_match

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

DLV = "dlv-thing-abc123"


def _repo(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True,
        **no_console_creationflags(),
    )
    return tmp_path


def _baton(root: Path, stub_id: str, **fields) -> str:
    lines = [
        "kind: roadmap-baton", f"title: {stub_id}", f"stub_id: {stub_id}",
        "status: open", "deployment_state: ready_to_fire", "baton_role: work",
    ]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    p = root / "state" / "handoffs" / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"state/handoffs/{stub_id}.md"


def _sizing(root: Path, name: str, deliverable_id: str, extra: str = "") -> str:
    rel = f"state/sizings/{name}.yaml"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"schema: sizing-object\ndeliverable_id: {deliverable_id}\nstatus: open\n{extra}",
        encoding="utf-8",
    )
    return rel


def _fm(root: Path, baton: str, key: str):
    split = split_frontmatter((root / baton).read_text(encoding="utf-8"))
    return read_fm_field_unquoted(split.fm_text, key)


def _land(root: Path, *entries: dict):
    return bl.land_wave(root, {"waveIndex": 0, "routedElsewhere": list(entries)})


def _xs(stub_id: str, **extra) -> dict:
    return {"batonId": stub_id, "route": "dispatch", "tshirt": "XS",
            "reason": "XS/dispatch, but no sizing object", **extra}


def test_an_unsized_xs_baton_is_linked_on_both_edges(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-1", deliverable_id=DLV)
    sizing = _sizing(root, "2026-10-22-thing", DLV)

    out = _land(root, _xs("b-1"))

    assert not out["refused"], out["refused"]
    assert out["xsUnlinked"] == []
    assert [(r["baton"], r["sizing"], r["basis"]) for r in out["xsLinked"]] == [
        (baton, sizing, "deliverable_id")
    ]
    assert f'baton: "{baton}"' in (root / sizing).read_text(encoding="utf-8")
    assert _fm(root, baton, "sizing_object") == sizing
    assert _fm(root, baton, "deliverable_id") == DLV


def test_a_baton_citing_a_sizing_without_a_baton_echo_is_stamped_directly(tmp_path):
    root = _repo(tmp_path)
    sizing = _sizing(root, "2026-10-22-other", "dlv-other-ffffff")
    baton = _baton(root, "b-2", deliverable_id="dlv-other-ffffff", sizing_object=sizing)

    out = _land(root, _xs("b-2"))

    assert out["xsLinked"][0]["basis"] == "cited"
    assert f'baton: "{baton}"' in (root / sizing).read_text(encoding="utf-8")


def test_an_ambiguous_match_leaves_both_files_byte_identical(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-3", deliverable_id=DLV)
    s1 = _sizing(root, "2026-10-22-a", DLV)
    s2 = _sizing(root, "2026-10-22-b", DLV)
    before = [(root / p).read_bytes() for p in (baton, s1, s2)]

    out = _land(root, _xs("b-3"))

    assert out["xsLinked"] == []
    assert out["xsUnlinked"][0]["basis"] == "ambiguous"
    assert [(root / p).read_bytes() for p in (baton, s1, s2)] == before


def test_an_s_tier_entry_is_not_touched(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-4", deliverable_id=DLV)
    sizing = _sizing(root, "2026-10-22-thing", DLV)
    before = [(root / p).read_bytes() for p in (baton, sizing)]

    out = _land(root, {"batonId": "b-4", "route": "dispatch", "tshirt": "S"})

    assert out["xsLinked"] == [] and out["xsUnlinked"] == []
    assert [(root / p).read_bytes() for p in (baton, sizing)] == before


def test_two_xs_entries_cost_one_git_spawn(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _baton(root, "b-5", deliverable_id=DLV)
    _baton(root, "b-6", deliverable_id="dlv-six-000006")
    _sizing(root, "2026-10-22-thing", DLV)
    _sizing(root, "2026-10-22-six", "dlv-six-000006")
    calls = []
    real = sizing_match.run_git

    def counting(args, **kw):
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(sizing_match, "run_git", counting)

    out = _land(root, _xs("b-5"), _xs("b-6"))

    assert len(out["xsLinked"]) == 2, out
    assert len(calls) == 1 and calls[0][0] == "grep"
