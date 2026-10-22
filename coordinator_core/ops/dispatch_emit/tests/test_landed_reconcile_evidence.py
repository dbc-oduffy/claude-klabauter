"""A `landed` inventory row stamps `coded` only on open rows whose declared writes a commit landed,
and never on a row behind an uncleared external gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.landed_reconcile import reconcile_landed_detail
from coordinator_core.session.record_homes import home_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN_REL = "docs/plans/leaf.md"


def _row(rid: str, extra: str = "") -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: code-edit\n  surface: src/{rid}.py\n"
        f"  writes: [src/{rid}.py]\n  disposition: open\n{extra}"
    )


_GATE = "  external_gates:\n    - requires: landed-work\n      cleared: false\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def inventory(tmp_path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    rows = _row("C1") + _row("C2") + _row("C4b", _GATE) + _row("C5", _GATE)
    (tmp_path / _PLAN_REL).write_text(
        f"---\nstatus: draft\n---\n\n# Leaf\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "plan")
    (tmp_path / "src").mkdir()
    for rid in ("C1", "C4b"):
        (tmp_path / "src" / f"{rid}.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "product")
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    inv = inv_dir / "run.md"
    inv.write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| leaf | `{_PLAN_REL}` | landed | `src/C1.py` | — | t | S | landed |\n",
        encoding="utf-8",
    )
    return inv


def test_only_the_evidenced_ungated_row_is_stamped(inventory, tmp_path):
    flipped, withheld = reconcile_landed_detail(inventory)

    assert flipped == {_PLAN_REL: ["C1"]}
    text = (tmp_path / _PLAN_REL).read_text(encoding="utf-8")
    assert text.count("disposition: coded") == 1
    assert text.count("disposition_ref") == 1


def test_an_unevidenced_row_is_withheld_and_reported(inventory, tmp_path):
    flipped, withheld = reconcile_landed_detail(inventory)

    assert "C2" not in flipped[_PLAN_REL]
    assert "C2" in withheld[_PLAN_REL]


def test_a_gated_row_is_never_stamped_even_when_its_writes_landed(inventory, tmp_path):
    flipped, withheld = reconcile_landed_detail(inventory)

    assert "C4b" not in flipped[_PLAN_REL] and "C5" not in flipped[_PLAN_REL]
    assert {"C4b", "C5"} <= set(withheld[_PLAN_REL])
