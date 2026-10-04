"""`--resume-from <continuance record>`: a fresh session re-emits each lane
inventory beside the record with every row whose plan spine row is already
`coded` left out -- no run id, no session state."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.session.record_homes import home_dir

_RUN = "20260930T224705-ecdd6226"

_PLAN = textwrap.dedent(
    """\
    ---
    status: draft
    ---

    # Plan

    ## Tasks

    ```yaml plan-tasks
    - id: C1
      title: landed in the cut-off run
      change_kind: code-edit
      surface: src/one.py
      writes: [src/one.py]
      disposition: coded
      disposition_ref: abc1234
    - id: C2
      title: still open
      change_kind: code-edit
      surface: src/two.py
      writes: [src/two.py]
      disposition: open
    - id: C3
      title: also open, needs C1
      change_kind: code-edit
      surface: src/three.py
      writes: [src/three.py]
      disposition: open
    ```
    """
)

_HEADER = (
    "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def _lane(path, rows: str):
    path.write_text(f"---\nrun_id: {_RUN}\n---\n\n## Chunk table\n\n{_HEADER}{rows}", encoding="utf-8")


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text(_PLAN, encoding="utf-8")
    inv = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv.mkdir(parents=True)
    _lane(
        inv / f"{_RUN}-a.md",
        "| P-C1 | `docs/plans/p.md` | one | `src/one.py` | — | t | S | queued |\n"
        "| P-C2 | `docs/plans/p.md` | two | `src/two.py` | — | t | S | queued |\n"
        "| P-C3 | `docs/plans/p.md` | three | `src/three.py` | P-C1 | t | S | queued |\n",
    )
    _lane(
        inv / f"{_RUN}-b.md",
        "| P-C1 | `docs/plans/p.md` | one | `src/one.py` | — | t | S | queued |\n",
    )
    (inv / f"{_RUN}-continuance.md").write_text(
        f"---\nkind: mise-continuance\nrun_id: {_RUN}\n---\n\n# CONTINUANCE\n", encoding="utf-8"
    )
    return inv


@pytest.fixture(autouse=True)
def _review_inputs(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def test_mint_with_skip_landed_drops_coded_plan_rows_and_their_edges(tmp_path):
    inv = _repo(tmp_path)
    skipped: list = []

    text, _ = im.mint_spine(str(inv / f"{_RUN}-a.md"), skip_landed=True, skipped_out=skipped)

    rows = {r["id"]: r for r in im.load_rows(text).rows}
    assert set(rows) == {"P-C2", "P-C3"}
    assert "depends_on" not in rows["P-C3"]
    assert skipped == ["P-C1"]


def test_mint_without_skip_landed_keeps_every_live_row(tmp_path):
    inv = _repo(tmp_path)

    text, _ = im.mint_spine(str(inv / f"{_RUN}-a.md"))

    assert {r["id"] for r in im.load_rows(text).rows} == {"P-C1", "P-C2", "P-C3"}


def test_resume_emits_each_lane_with_unlanded_rows_and_names_the_rest(tmp_path, capsys):
    inv = _repo(tmp_path)

    rc = cli_module.main(
        [
            "--resume-from", str(inv / f"{_RUN}-continuance.md"),
            "--out", str(tmp_path / "resume.workflow.mjs"),
            "--repo-root", str(tmp_path),
        ]
    )

    assert rc == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    by_lane = {lane["lane"]: lane for lane in reply["lanes"]}
    assert by_lane["a"]["resume_skipped_landed"] == ["P-C1"]
    assert (tmp_path / "resume-a.workflow.mjs").is_file()
    assert by_lane["b"]["nothing_unlanded"] is True
    assert not (tmp_path / "resume-b.workflow.mjs").exists()
    assert not (inv / f"{_RUN}-b.spine.md").exists()


def test_resume_from_a_record_with_no_lane_inventory_is_refused(tmp_path):
    (tmp_path / ".git").mkdir()
    inv = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv.mkdir(parents=True)
    record = inv / f"{_RUN}-continuance.md"
    record.write_text(f"---\nrun_id: {_RUN}\n---\n", encoding="utf-8")

    rc = cli_module.main(
        ["--resume-from", str(record), "--out", str(tmp_path / "r.workflow.mjs"), "--repo-root", str(tmp_path)]
    )

    assert rc == cli_module.EXIT_DATA_ERROR
