"""Pins for the plan-completion ledger and the commit-sha seam on `GitResult`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core import plan_completion_ledger as led

_NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _row(plan: str, at: str, sha: str = "a" * 40) -> dict:
    return led.make_row(plan, "landed", at, sha, b"plan bytes", "M", ["src/a.py"])


def test_append_writes_lf_one_object_per_line_forward_slashes(tmp_path: Path) -> None:
    led.append_row(tmp_path, _row("docs\\plans\\a.md", "2026-11-14T10:00:00Z"))
    led.append_row(tmp_path, _row("docs/plans/b.md", "2026-11-14T11:00:00Z"))
    raw = (tmp_path / led.LEDGER).read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    parsed = [json.loads(line) for line in raw.decode("ascii").split("\n") if line]
    assert [r["plan"] for r in parsed] == ["docs/plans/a.md", "docs/plans/b.md"]
    assert set(parsed[0]) == set(led.ROW_KEYS)
    assert parsed[0]["plan_blob"] == led.blob_id(b"plan bytes")


def test_append_refuses_an_incomplete_or_foreign_row(tmp_path: Path) -> None:
    bad = _row("p.md", "2026-11-14T10:00:00Z")
    del bad["plan_blob"]
    with pytest.raises(ValueError):
        led.append_row(tmp_path, bad)
    foreign = _row("p.md", "2026-11-14T10:00:00Z")
    foreign["v"] = 2
    with pytest.raises(ValueError):
        led.append_row(tmp_path, foreign)
    assert not (tmp_path / led.LEDGER).exists()


def test_read_window_admits_inside_and_refuses_outside_both_sides(tmp_path: Path) -> None:
    led.append_row(tmp_path, _row("old.md", "2026-11-01T00:00:00Z"))
    led.append_row(tmp_path, _row("edge.md", "2026-11-07T12:00:00Z"))
    led.append_row(tmp_path, _row("new.md", "2026-11-14T08:00:00+00:00"))
    got = [r["plan"] for r in led.read_window(tmp_path, None, days=7, now=_NOW)]
    assert got == ["edge.md", "new.md"]
    got = [r["plan"] for r in led.read_window(tmp_path, "2026-11-10", now=_NOW)]
    assert got == ["new.md"]
    got = [r["plan"] for r in led.read_window(tmp_path, "2026-10-31T00:00:00Z", now=_NOW)]
    assert got == ["old.md", "edge.md", "new.md"]


def test_read_window_missing_ledger_and_malformed_lines(tmp_path: Path) -> None:
    assert led.read_window(tmp_path, None, now=_NOW) == []
    led.append_row(tmp_path, _row("ok.md", "2026-11-14T08:00:00Z"))
    with open(tmp_path / led.LEDGER, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("not json\n\n[1]\n" + json.dumps({"schema": "other", "completed_at": "2026-11-14T09:00:00Z"}) + "\n")
    assert [r["plan"] for r in led.read_window(tmp_path, None, now=_NOW)] == ["ok.md"]
    with pytest.raises(ValueError):
        led.read_window(tmp_path, "yesterday-ish", now=_NOW)


def test_spine_writes_block_list_and_inline_forms_in_order_deduped() -> None:
    text = (
        "# plan\n\n```yaml plan-tasks\n"
        "- id: C0\n  writes:\n    - src\\a.py\n    - ./src/b.py\n  body: y\n"
        "- id: C1\n  writes: [src/b.py, \"src/c.py\"]\n  body: x\n```\n\n"
        "```yaml\nwrites:\n  - ignored.py\n```\n"
    )
    assert led.spine_writes(text) == ["src/a.py", "src/b.py", "src/c.py"]
    assert led.spine_writes("no spine here") == []


def test_tshirt_reads_estimate_block_and_refuses_unknown_or_missing(tmp_path: Path) -> None:
    (tmp_path / "s.yaml").write_text("id: x\nestimate:\n  tshirt: 'l'\nother:\n  tshirt: XL\n", encoding="utf-8")
    assert led.tshirt(tmp_path, 'title: t\nsizing_object: "s.yaml"\n') == "L"
    (tmp_path / "bad.yaml").write_text("estimate:\n  tshirt: HUGE\n", encoding="utf-8")
    assert led.tshirt(tmp_path, "sizing_object: bad.yaml\n") is None
    assert led.tshirt(tmp_path, "sizing_object: gone.yaml\n") is None
    assert led.tshirt(tmp_path, "sizing_object: null\n") is None
    assert led.tshirt(tmp_path, "title: t\n") is None


def test_frontmatter_and_body_split() -> None:
    text = "---\nstatus: landed\n---\n\n# H\n"
    assert led.frontmatter(text).strip() == "status: landed"
    assert led.body(text) == "# H\n"
    assert led.frontmatter("# H\n") == "" and led.body("# H\n") == "# H\n"


def test_completed_statuses_are_members_of_the_plan_schema_status_enum() -> None:
    schema_path = Path(led.__file__).parent / "frontmatter" / "schemas" / "plan.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert led.COMPLETED_STATUSES <= set(schema["properties"]["status"]["enum"])
    assert "complete" not in led.COMPLETED_STATUSES
