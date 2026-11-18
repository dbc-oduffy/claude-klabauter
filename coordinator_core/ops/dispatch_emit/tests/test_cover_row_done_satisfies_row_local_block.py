"""A cover row's DONE edit of a path answers another row's ROW-LOCAL-BLOCK on it: rows held behind the
blocked row are not "unexecuted plan rows", so the executed rows' code is not withheld."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow
from coordinator_core.ops.dispatch_emit.unexecuted_plan_rows import satisfied_blocks, unexecuted_plan_rows

PATH = "scan_truth/head_pass_join.py"


def _row(row_id: str, wave: int, deps: tuple = ()) -> ManifestRow:
    return ManifestRow(
        id=row_id, agent_type="coordinator:executor", model="sonnet", brief_path=f"{row_id}.md",
        writes=(f"{row_id}.py",), wave=wave, deps=deps,
    )


def _chunk(row_id: str, paths: tuple) -> SimpleNamespace:
    return SimpleNamespace(id=row_id, report=f"reports/{row_id}.md", paths=paths)


ROWS = (_row("C2", 0), _row("W1", 0), _row("LR4", 1, ("C2",)), _row("LR5", 2, ("LR4",)), _row("LR9", 1))
CHUNKS = (_chunk("C2", ("C2.py",)), _chunk("W1", (PATH,)))
BLOCKED = f"BLOCKED: footprint\nROW-LOCAL-BLOCK: {PATH}\n"


def _reports(c2: str, w1: str) -> dict:
    return {"reports/C2.md": c2, "reports/W1.md": w1}


def test_done_cover_row_satisfies_the_block_and_frees_the_rows_held_behind_it() -> None:
    reports = _reports(BLOCKED, "DONE: W1\n")
    freed = satisfied_blocks(CHUNKS, reports.get)

    assert freed == ("C2",)
    assert unexecuted_plan_rows(ROWS, ["C2", "W1"], freed) == ("LR9",)


def test_without_the_cover_row_done_the_held_rows_stay_unexecuted() -> None:
    for w1 in ("BLOCKED: nope\n", None):
        reports = _reports(BLOCKED, w1) if w1 else {"reports/C2.md": BLOCKED}
        freed = satisfied_blocks(CHUNKS, reports.get)

        assert freed == ()
        assert unexecuted_plan_rows(ROWS, ["C2", "W1"], freed) == ("LR4", "LR5", "LR9")


def test_a_cover_row_writing_another_path_does_not_satisfy_the_block() -> None:
    chunks = (_chunk("C2", ("C2.py",)), _chunk("W1", ("other.py",)))
    reports = _reports(BLOCKED, "DONE: W1\n")

    assert satisfied_blocks(chunks, reports.get) == ()


def test_a_row_never_satisfies_its_own_block() -> None:
    chunks = (_chunk("C2", (PATH,)),)

    assert satisfied_blocks(chunks, {"reports/C2.md": BLOCKED + "\nDONE\n"}.get) == ()
