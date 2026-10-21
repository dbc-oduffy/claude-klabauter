"""stranded_part_rebuild writes a landing marker into a pre-landing_markers receipt (AC7)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.emission_receipt import (
    emission_receipt_path,
    receipt_landing_text,
)
from coordinator_core.ops.dispatch_emit.stranded_part_rebuild import (
    StrandedPartRebuildError,
    main,
    rebuild_stranded_part,
)

_BASE = "20261021T182452-4b88be1d"
_SCRIPT = f"state/mise-inventory/{_BASE}-t2-p2.workflow.mjs"
_SPINE = f"state/mise-inventory/{_BASE}-t2-p2.spine.md"

_INVENTORY = textwrap.dedent(
    """\
    ---
    run_id: 20261021T182452-4b88be1d-t2
    ---

    ## Chunk table

    | id | spec path | summary | footprint | deps | verification | complexity | disposition |
    |---|---|---|---|---|---|---|---|
    | C1 | `docs/plans/fixture.md` | first | `pkg/one.py` | — | scoped pytest | S | in_progress |
    | C2 | `docs/plans/fixture.md` | second | `pkg/two.py` | C1 | scoped pytest | S | in_progress |
    | C3 | `docs/plans/fixture.md` | third | `pkg/three.py` | — | scoped pytest | S | in_progress |
    """
)


def _setup(tmp_path: Path, receipt: dict | None = None, script_name: str = _SCRIPT):
    inv = tmp_path / "state/mise-inventory"
    inv.mkdir(parents=True)
    inventory = inv / f"{_BASE}-t2.md"
    inventory.write_text(_INVENTORY, encoding="utf-8")
    base = {"sha256": "ab", "session_id": "sess-1", "plan_relpath": _SPINE}
    receipt_file = emission_receipt_path(tmp_path / _SCRIPT)
    receipt_file.write_text(json.dumps(base if receipt is None else receipt), encoding="utf-8")
    out = tmp_path / "task.output"
    out.write_text(
        json.dumps(
            {
                "result": {
                    "chunks": ["C2", "C3"],
                    "plan": {"path": _SPINE, "deliverable_id": None},
                    "next_action": {"params": {"script_path": script_name}},
                }
            }
        ),
        encoding="utf-8",
    )
    return inventory, out


def test_rebuild_writes_markers_and_spine_that_parse_back(tmp_path):
    inventory, out = _setup(tmp_path)
    before = json.loads(emission_receipt_path(tmp_path / _SCRIPT).read_text(encoding="utf-8"))

    result = rebuild_stranded_part(tmp_path, _SCRIPT, out, inventory)

    assert result["spine_written"] is True
    receipt = json.loads(emission_receipt_path(tmp_path / _SCRIPT).read_text(encoding="utf-8"))
    for key, value in before.items():
        assert receipt[key] == value
    assert set(receipt["rebuilt_from"]) == {"inventory", "task_output", "at"}
    request = parse_marker(receipt_landing_text(tmp_path / _SCRIPT))
    assert [c.id for c in request.chunks] == ["C2", "C3"]
    assert request.plan_path == _SPINE
    assert request.session_id == "sess-1"
    spine = (tmp_path / _SPINE).read_text(encoding="utf-8")
    assert "id: C2" in spine and "id: C3" in spine and "id: C1" not in spine
    assert "chunk: C1" not in spine


def test_existing_spine_is_left_alone(tmp_path):
    inventory, out = _setup(tmp_path)
    (tmp_path / _SPINE).write_text("kept", encoding="utf-8")
    result = rebuild_stranded_part(tmp_path, _SCRIPT, out, inventory)
    assert result["spine_written"] is False
    assert (tmp_path / _SPINE).read_text(encoding="utf-8") == "kept"


@pytest.mark.parametrize("key", ["landing_markers", "landed"])
def test_refuses_a_receipt_already_marked_or_landed(tmp_path, key):
    inventory, out = _setup(tmp_path, {"sha256": "ab", key: ["x"] if key == "landing_markers" else {"sha": "s"}})
    with pytest.raises(StrandedPartRebuildError, match=key):
        rebuild_stranded_part(tmp_path, _SCRIPT, out, inventory)


def test_refuses_an_absent_receipt(tmp_path):
    inventory, out = _setup(tmp_path)
    emission_receipt_path(tmp_path / _SCRIPT).unlink()
    with pytest.raises(StrandedPartRebuildError, match="no readable receipt"):
        rebuild_stranded_part(tmp_path, _SCRIPT, out, inventory)


def test_refuses_a_task_output_for_another_script(tmp_path):
    inventory, out = _setup(tmp_path, script_name=_SCRIPT.replace("-p2", "-p1"))
    with pytest.raises(StrandedPartRebuildError, match="names script"):
        rebuild_stranded_part(tmp_path, _SCRIPT, out, inventory)


def test_refuses_an_unresolved_chunk_id_and_writes_nothing(tmp_path):
    inventory, out = _setup(tmp_path)
    digest = json.loads(out.read_text(encoding="utf-8"))
    digest["result"]["chunks"] = ["C2", "C9"]
    out.write_text(json.dumps(digest), encoding="utf-8")
    with pytest.raises(StrandedPartRebuildError, match="C9"):
        rebuild_stranded_part(tmp_path, _SCRIPT, out, inventory)
    assert receipt_landing_text(tmp_path / _SCRIPT) is None
    assert not (tmp_path / _SPINE).exists()


def test_cli_refusal_exits_nonzero(tmp_path, capsys):
    inventory, out = _setup(tmp_path)
    emission_receipt_path(tmp_path / _SCRIPT).unlink()
    code = main(
        ["--script", _SCRIPT, "--task-output", str(out), "--inventory", str(inventory),
         "--worktree-root", str(tmp_path)]
    )
    assert code == 1
    assert "refused" in capsys.readouterr().err
