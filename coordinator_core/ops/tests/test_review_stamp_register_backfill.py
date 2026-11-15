"""review-stamp register --backfill-under: sweeps implemented plans lacking review_stamp.register."""
from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops import review_stamp as m

OK_SIZING = "state/sizings/ok.yaml"
OWED_SIZING = "state/sizings/owed.yaml"
STAMP = (
    "review_stamp:\n  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
    "  criterion:\n    status: met\n    observation: o\n    sidecar: null\n"
    "  stamped_at: '2026-10-01T00:00:00Z'\n"
)


def _row(rid, status="met", wired=True):
    return {
        "id": rid, "source_text": f"text {rid}", "anchor": "§1", "surface": "internal",
        "status": status, "claimed_by": [], "wired": wired, "met_by": [],
        "last_progress_at": None, "ruling": None,
    }


def _sizing(root: Path, rel: str, rows) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: sizing-object\n" + yaml.safe_dump(
            {"requirement_register": {"sources": [{"kind": "pm"}], "rows": rows}}, sort_keys=False),
        encoding="utf-8")


def _plan(root: Path, name: str, sizing: str, *, extra: str = "", status="implemented") -> Path:
    fm = f"title: T\nstatus: {status}\nregister_claims:\n  sizing: {sizing}\n  rows: [A]\n{extra}"
    path = root / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    return path


def _stamp(plan: Path) -> dict:
    return yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])["review_stamp"]


def _fixture(tmp_path: Path):
    _sizing(tmp_path, OK_SIZING, [_row("A")])
    _sizing(tmp_path, OWED_SIZING, [_row("A", "open", False)])
    lacking = _plan(tmp_path, "lacking.md", OK_SIZING, extra=STAMP)
    carrying = _plan(tmp_path, "carrying.md", OK_SIZING, extra=STAMP)
    assert m.write_register(carrying, tmp_path)["status"] == "registered"
    owed = _plan(tmp_path, "owed.md", OWED_SIZING, extra=STAMP)
    executing = _plan(tmp_path, "executing.md", OK_SIZING, extra=STAMP, status="executing")
    return lacking, carrying, owed, executing


def test_sweep_backfills_skips_and_reports(tmp_path):
    lacking, carrying, owed, executing = _fixture(tmp_path)
    carrying_before, owed_before, executing_before = (
        p.read_bytes() for p in (carrying, owed, executing))

    result = m.backfill_register(tmp_path / "plans", tmp_path)

    assert result["registered"] == [str(lacking)]
    assert _stamp(lacking)["register"]["sizing"] == OK_SIZING
    assert [p for p, _ in result["refused"]] == [str(owed)]
    assert "open=1" in result["refused"][0][1]
    assert carrying.read_bytes() == carrying_before
    assert owed.read_bytes() == owed_before
    assert executing.read_bytes() == executing_before


def test_sweep_is_idempotent(tmp_path):
    lacking, *_ = _fixture(tmp_path)
    m.backfill_register(tmp_path / "plans", tmp_path)
    after = lacking.read_bytes()
    again = m.backfill_register(tmp_path / "plans", tmp_path)
    assert again["registered"] == []
    assert lacking.read_bytes() == after


def test_cli_exit_nonzero_naming_refused(tmp_path, capsys):
    lacking, _, owed, _ = _fixture(tmp_path)
    rc = m.main(["register", "--backfill-under", str(tmp_path / "plans"), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert str(owed) in err
    assert "register" in _stamp(lacking)


def test_cli_exit_zero_when_nothing_refused(tmp_path):
    _sizing(tmp_path, OK_SIZING, [_row("A")])
    lacking = _plan(tmp_path, "lacking.md", OK_SIZING, extra=STAMP)
    rc = m.main(["register", "--backfill-under", str(tmp_path / "plans"), "--repo-root", str(tmp_path)])
    assert rc == 0
    assert "register" in _stamp(lacking)


def test_single_plan_form_still_works(tmp_path):
    _sizing(tmp_path, OK_SIZING, [_row("A")])
    plan = _plan(tmp_path, "one.md", OK_SIZING, extra=STAMP)
    assert m.main(["register", "--plan", str(plan), "--repo-root", str(tmp_path)]) == 0
    assert "register" in _stamp(plan)


def test_sweep_reports_unparseable_frontmatter_without_aborting(tmp_path):
    lacking, _, _, _ = _fixture(tmp_path)
    bad = tmp_path / "plans" / "corrupt.md"
    bad.write_text("---\ntitle: [unclosed\nstatus: implemented\n---\n\nBody.\n", encoding="utf-8")
    plain = tmp_path / "plans" / "notes.md"
    plain.write_text("no frontmatter\n", encoding="utf-8")
    result = m.backfill_register(tmp_path / "plans", tmp_path)
    assert result["unreadable"] == [str(bad)]
    assert str(lacking) in result["registered"]
