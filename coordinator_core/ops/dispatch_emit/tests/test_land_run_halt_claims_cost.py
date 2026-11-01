"""The halted landing's superseded-run scan reads run.json for the sizing's own era only.

Base (HEAD before the change), the `land_run_halt._land.claims` leg start: `_superseded_candidates`
offered every earlier `ask-*` record dir, so the scan read one run.json per dir in the corpus
(2294 on this tree; 600 in this fixture) before the claims leg could start. After: only dirs stamped
inside the sizing's era whose run.json names the sizing are read-and-kept.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import land_run_halt

SIZING = "state/sizings/2026-10-31-a6c83f-2-hang.yaml"
RUN_ID = "ask-20261101T000000-aaaaaa"


def _dir(root: Path, name: str, sizing: str) -> None:
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")


def _counting_reads(monkeypatch) -> list:
    reads: list = []
    real = Path.read_bytes

    def spy(self):
        if self.name == "run.json":
            reads.append(self.parent.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    return reads


def test_reads_follow_the_sizing_era_not_the_corpus(tmp_path, monkeypatch):
    for i in range(600):
        _dir(tmp_path, f"ask-20260{(i % 9) + 1}01T0000{i % 60:02d}-{i:06x}", "state/sizings/other.yaml")
    _dir(tmp_path, "ask-20261031T100000-bbbbbb", SIZING)
    _dir(tmp_path, "ask-20261031T110000-cccccc", "state/sizings/2026-10-31-other.yaml")
    _dir(tmp_path, "ask-20261031T120000-dddddd", f"{SIZING}")
    # Binds the sizing but stamped before the sizing's creation day: cannot be a real binding.
    _dir(tmp_path, "ask-20261001T120000-eeeeee", SIZING)
    # Not earlier than this run.
    _dir(tmp_path, "ask-20261101T120000-ffffff", SIZING)
    reads = _counting_reads(monkeypatch)

    found = land_run_halt._superseded_candidates(tmp_path, RUN_ID, SIZING)

    assert found == ["ask-20261031T100000-bbbbbb", "ask-20261031T120000-dddddd"]
    assert len(reads) <= 3, reads


def test_warp_dirs_for_the_sizing_stem_are_kept_without_a_read(tmp_path, monkeypatch):
    _dir(tmp_path, "warp-2026-10-31-a6c83f-2-hang-20261031T090000-abcdef", SIZING)
    _dir(tmp_path, "warp-2026-10-31-other-20261031T090000-abcdef", SIZING)
    reads = _counting_reads(monkeypatch)

    found = land_run_halt._superseded_candidates(tmp_path, RUN_ID, SIZING)

    assert found == ["warp-2026-10-31-a6c83f-2-hang-20261031T090000-abcdef"]
    assert reads == []
