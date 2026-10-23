"""A sha-bearing revise note that also rules new work still derives the row carrying that work."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import derive_ledger_rows

_J1 = (
    "Add gem_holder() to coordinator_core/group_em/gem_home.py and switch gem-triage-turn-intent.py "
    "to it. Work landed in 71ebad6c6a meets the rows it names."
)
_SERVING = {"R1": ["coordinator/bin/gem-triage-turn-intent.py"]}


def _touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


def test_sha_note_with_remaining_derives_row(tmp_path):
    _touch(tmp_path, "coordinator/bin/gem-triage-turn-intent.py")
    rows, under = derive_ledger_rows(
        {"R1": {"remaining": ["gem-triage-turn-intent.py still depends on hooks _gem_home"]}},
        [_J1], ["R1"], _SERVING, [], tmp_path)
    assert under == [] and len(rows) == 1
    assert rows[0]["register_rows"] == ["R1"]
    assert "coordinator/bin/gem-triage-turn-intent.py" in rows[0]["writes"]
    assert "71ebad6c6a" in rows[0]["body"]


def test_sha_note_without_remaining_derives_nothing(tmp_path):
    _touch(tmp_path, "coordinator/bin/gem-triage-turn-intent.py")
    rows, under = derive_ledger_rows({"R1": {"remaining": []}}, [_J1], ["R1"], _SERVING, [], tmp_path)
    assert (rows, under) == ([], [])
