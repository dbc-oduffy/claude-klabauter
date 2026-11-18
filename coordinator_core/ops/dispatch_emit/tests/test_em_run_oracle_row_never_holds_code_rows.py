"""An EM-run oracle row stays open for met_by crediting and holds no code row behind it."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import derive_ledger_rows
from coordinator_core.ops.dispatch_emit.spine_read import read_spine

_ORACLE_TITLE = "Ada stage 2 exit 0 (EM-run oracle)"


def _row(rid: str, title: str, writes: str, *, deps: tuple = (), extra: str = "") -> str:
    dep = "  depends_on:\n" if deps else ""
    dep += "".join(f"    - chunk: {d}\n      gate_kind: output-consumption-runtime\n" for d in deps)
    return (
        f"- id: {rid}\n  title: '{title}'\n  change_kind: code-edit\n  surface: {writes}\n"
        f"  writes:\n    - {writes}\n  agent_type: coordinator:executor\n{extra}{dep}"
        "  body: |\n    do it\n"
    )


def _oracle(rid: str = "LR3") -> str:
    return (
        f"- id: {rid}\n  title: '{_ORACLE_TITLE}'\n  change_kind: verification\n  surface: pkg/a.py\n"
        "  writes: []\n  em_run: true\n  agent_type: coordinator:executor\n  body: |\n    run the oracle\n"
    )


def _plan(tmp_path: Path, rows: str) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )
    return plan


def test_code_rows_behind_an_em_run_oracle_row_dispatch(tmp_path):
    rows = (
        _oracle()
        + _row("LR5", "five", "pkg/five.py", deps=("LR3",))
        + _row("LR6", "six", "pkg/six.py", deps=("LR3", "LR5"))
    )
    exclusions: list = []
    dispatched = read_spine(_plan(tmp_path, rows), exclusions=exclusions)
    assert [r.id for r in dispatched] == ["LR5", "LR6"]
    assert all(e.get("chunk") != "LR3" for r in dispatched for e in r.depends_on)
    assert [(e["id"], e["reason"]) for e in exclusions] == [("LR3", "em-performed")]


def test_a_derived_gap_row_naming_an_oracle_is_open_not_dispatched(tmp_path):
    gap = _row("LR3", "Close gap: " + _ORACLE_TITLE, "pkg/a.py")
    rows = gap + _row("LR5", "five", "pkg/five.py", deps=("LR3",))
    exclusions: list = []
    dispatched = read_spine(_plan(tmp_path, rows), exclusions=exclusions)
    assert [r.id for r in dispatched] == ["LR5"]
    assert [(e["id"], e["reason"]) for e in exclusions] == [("LR3", "em-performed")]


def test_a_non_oracle_em_row_still_withholds_its_code_rows(tmp_path):
    em = _oracle().replace(_ORACLE_TITLE, "send the memo").replace("run the oracle", "send it")
    rows = em + _row("LR5", "five", "pkg/five.py", deps=("LR3",))
    exclusions: list = []
    assert read_spine(_plan(tmp_path, rows), exclusions=exclusions) == []
    assert {e["id"]: e["reason"] for e in exclusions} == {"LR3": "em-performed", "LR5": "withheld_by_owed_dependency"}


def test_a_non_code_row_behind_the_oracle_stays_withheld(tmp_path):
    doc = (
        "- id: D1\n  title: doc\n  change_kind: doc-edit\n  surface: docs/x/d1.md\n"
        "  writes:\n    - docs/x/d1.md\n  depends_on:\n    - chunk: LR3\n      gate_kind: output-consumption-runtime\n"
    )
    exclusions: list = []
    assert read_spine(_plan(tmp_path, _oracle() + doc), exclusions=exclusions) == []
    assert {e["id"]: e["reason"] for e in exclusions} == {"LR3": "em-performed", "D1": "withheld_by_owed_dependency"}


def test_derived_row_title_reads_oracle_only_when_every_gap_is_one(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/a.py").write_text("x\n", encoding="utf-8")
    serving = {"R3": ["pkg/a.py"]}

    def title(steps):
        rows, _ = derive_ledger_rows({"R3": {"remaining": steps}}, [], ["R3"], serving, [], tmp_path)
        return rows[0]["title"]

    assert title([_ORACLE_TITLE]) == "Close gap: " + _ORACLE_TITLE
    assert title([_ORACLE_TITLE, "pkg/a.py lacks the fix"]) == "Close gap: pkg/a.py lacks the fix"
