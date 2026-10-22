"""
coordinator_core/ops/tests/test_plan_record_residue_rulings.py — "plan.record_residue_rulings".

Claim under test: each APM ruling lands in its plan's ``pm_ruling`` list, merged by
item id, and a ruling that cannot land is refused in the reply, never raised.
Fixtures are a bare ``tmp_path`` tree with a ``.git`` directory (no ``git init``).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import ipc
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops import plan_record_residue_rulings as mod
from coordinator_core.roadmap import mise_residue as mr

OP_KEY = "plan.record_residue_rulings"
REL = "docs/plans/2026-10-22-fixture.md"
REL2 = "docs/plans/2026-10-22-other.md"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    return tmp_path / ".git"


def _write(root: Path, rel: str, extra: str = "") -> Path:
    p = root / rel
    p.write_text(
        "---\ntitle: fixture\nstatus: draft\n" + extra + "---\n\n# Body\n", encoding="utf-8"
    )
    return p


def _fm(path: Path) -> dict:
    return parse_frontmatter(path.read_text(encoding="utf-8"))["frontmatter"]


def _ruling(rid="census[0]", plan=REL, **kw) -> dict:
    return {
        "id": rid,
        "item_kind": "census-leftover",
        "plan": plan,
        "ruling": "accept as is",
        "pmOnly": False,
        "subjectSha": "abc123def456",
        **kw,
    }


def _run(common: Path, rulings: list) -> dict:
    return mod._handler({"run_id": "r1", "on": "2026-10-22", "rulings": rulings}, common)


def test_op_resolves_and_is_common_dir_keyed():
    assert callable(ipc._REGISTRY.get(OP_KEY) or ipc._lazy_import_and_lookup(OP_KEY))
    assert ipc.OP_KEY_SCOPE.get(OP_KEY) == "common_dir"


def test_a_ruling_lands_in_the_plan_and_reads_back_as_ruled(tmp_path):
    common = _repo(tmp_path)
    plan = _write(tmp_path, REL)
    reply = _run(common, [_ruling(pmOnly=True)])
    assert reply == {"applied": [{"plan": REL, "id": "census[0]"}], "refused": []}
    entry = _fm(plan)["pm_ruling"][0]
    assert entry["item"] == "census[0]"
    assert entry["on"] == "2026-10-22"
    assert entry["pm_only"] is True
    assert entry["subject_sha"] == "abc123def456"
    assert entry["run_id"] == "r1"
    assert _fm(plan)["status"] == "draft"


def test_equal_id_replaces_and_other_entries_survive(tmp_path):
    common = _repo(tmp_path)
    plan = _write(tmp_path, REL)
    _run(common, [_ruling("census[0]"), _ruling("census[1]")])
    _run(common, [_ruling("census[0]", ruling="changed my mind")])
    block = _fm(plan)["pm_ruling"]
    assert [e["item"] for e in block] == ["census[0]", "census[1]"]
    assert block[0]["ruling"] == "changed my mind"


def test_two_plans_are_each_written(tmp_path):
    common = _repo(tmp_path)
    a, b = _write(tmp_path, REL), _write(tmp_path, REL2)
    reply = _run(common, [_ruling(plan=REL), _ruling("census[3]", plan=REL2)])
    assert len(reply["applied"]) == 2 and reply["refused"] == []
    assert _fm(a)["pm_ruling"][0]["item"] == "census[0]"
    assert _fm(b)["pm_ruling"][0]["item"] == "census[3]"


def test_a_missing_plan_is_refused_not_raised(tmp_path):
    common = _repo(tmp_path)
    reply = _run(common, [_ruling(plan="docs/plans/nope.md")])
    assert reply["applied"] == []
    assert reply["refused"][0]["reason"] == "no such plan"


def test_an_uncontained_plan_is_refused_and_not_written(tmp_path):
    common = _repo(tmp_path)
    outside = tmp_path.parent / "elsewhere-rulings.md"
    outside.write_text("---\ntitle: x\n---\n", encoding="utf-8")
    reply = _run(common, [_ruling(plan=str(outside))])
    assert reply["applied"] == [] and "escapes" in reply["refused"][0]["reason"]
    assert "pm_ruling" not in outside.read_text(encoding="utf-8")


def test_an_empty_ruling_is_refused_and_nothing_is_written(tmp_path):
    common = _repo(tmp_path)
    plan = _write(tmp_path, REL)
    before = plan.read_text(encoding="utf-8")
    reply = _run(common, [_ruling(ruling="   ")])
    assert reply["applied"] == [] and reply["refused"][0]["reason"] == "empty ruling"
    assert plan.read_text(encoding="utf-8") == before


def test_a_cycle_ruling_needs_its_plan_in_cycle_plans(tmp_path):
    common = _repo(tmp_path)
    plan = _write(tmp_path, REL)
    refused = _run(
        common,
        [_ruling("cycle:a.py", item_kind="seam-fix-cycle", cycle_plans=[REL2])],
    )
    assert refused["applied"] == [] and "cycle" in refused["refused"][0]["reason"]
    assert "pm_ruling" not in _fm(plan)
    ok = _run(
        common,
        [_ruling("cycle:a.py", item_kind="seam-fix-cycle", cycle_plans=[REL, REL2])],
    )
    assert ok["applied"] == [{"plan": REL, "id": "cycle:a.py"}]
    assert _fm(plan)["pm_ruling"][0]["item"] == "cycle:a.py"


def test_a_good_and_a_bad_ruling_in_one_call_split_the_reply(tmp_path):
    common = _repo(tmp_path)
    _write(tmp_path, REL)
    reply = _run(common, [_ruling(), _ruling("census[1]", ruling="")])
    assert [a["id"] for a in reply["applied"]] == ["census[0]"]
    assert [r["id"] for r in reply["refused"]] == ["census[1]"]


def test_the_written_block_is_what_ruled_items_reads(tmp_path):
    common = _repo(tmp_path)
    item = {"kind": "census-leftover", "index": 0, "command": "run x"}
    plan = _write(tmp_path, REL, extra="census:\n  - command: run x\n")
    _run(common, [_ruling(mr.ruling_item_id(item), subjectSha=mr.subject_sha(item))])
    assert mr.ruled_items(plan.read_text(encoding="utf-8")) == frozenset({"census[0]"})
