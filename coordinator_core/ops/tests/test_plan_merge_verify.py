"""plan.merge body: carry, merge, six checks, write once. rekey/delta helpers are stubbed; pure tmp_path."""

from __future__ import annotations

import json
import sys
import types

import pytest

from coordinator_core.ops._plan_merge_contract import CHECK_KEYS, RowAccount
from coordinator_core.ops.plan_merge import _handler


@pytest.fixture(autouse=True)
def _git_dir(tmp_path):
    """main_worktree_root refuses a root with no .git entry."""
    (tmp_path / ".git").mkdir(exist_ok=True)


def _row(rid, extra="", body="work"):
    return (
        f"- id: {rid}\n  title: \"t {rid}\"\n  change_kind: code-edit\n  surface: x.py\n  writes: []\n"
        f"{extra}  body: |\n    {body}\n"
    )


def _plan(rows, pre="# Title\n\nintro\n", extra_section="", heading="Tasks", title="A"):
    return (
        f"---\ntitle: \"{title}\"\ncreated: \"2026-10-22\"\nauthor: EM\nstatus: draft\n---\n\n{pre}\n"
        f"{extra_section}## {heading}\n\n```yaml plan-tasks\n" + "".join(rows) + "```\n"
    )


def _delta(**over):
    d = {
        "sources": [{"path": "a.md", "origin_label": "a"}, {"path": "b.md", "origin_label": "b"}],
        "frontmatter": {"title": "M", "created": "2026-10-22", "author": "EM", "status": "draft"},
        "dropped_sections": [
            {"heading": "(preamble)", "source": "a.md", "reason": "r"},
            {"heading": "(preamble)", "source": "b.md", "reason": "r"},
        ],
    }
    d.update(over)
    return d


def _stub_helpers(monkeypatch, apply_delta=None):
    rekey = types.ModuleType("coordinator_core.ops._plan_merge_rekey")
    rekey.detect_collisions = lambda rows: []
    rekey.apply_rekey = lambda rows, mapping: list(rows)
    delta = types.ModuleType("coordinator_core.ops._plan_merge_delta")

    def default_apply(rows, d):
        return [r.text for r in rows], [RowAccount(r.row_id, r.source, "untouched") for r in rows]

    delta.apply_delta = apply_delta or default_apply
    delta.render_delta_tables = lambda accounts, dropped, reasons=None: "| Row | Source | Edit classes | Reason |\n"
    monkeypatch.setitem(sys.modules, "coordinator_core.ops._plan_merge_rekey", rekey)
    monkeypatch.setitem(sys.modules, "coordinator_core.ops._plan_merge_delta", delta)


def _write(tmp_path, a=None, b=None, delta=None, nl="\n"):
    a = a if a is not None else _plan([_row("A1"), _row("A2")], title="A")
    b = b if b is not None else _plan([_row("B1")], title="B")
    for name, text in (("a.md", a), ("b.md", b)):
        with open(tmp_path / name, "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace("\n", nl))
    (tmp_path / "d.json").write_text(json.dumps(delta if delta is not None else _delta()), encoding="utf-8")


def _run(tmp_path, **params):
    p = {"sources": ["a.md", "b.md"], "delta": "d.json", "out": "o.md"}
    p.update(params)
    return _handler(p, tmp_path)


def test_clean_merge_writes_once_and_reports(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    _write(tmp_path)
    res = _run(tmp_path)
    assert res["ok"] is True, res
    counts = res["report"]["counts"]
    assert (counts["source_rows"], counts["untouched"], counts["unaccounted"], counts["out"]) == (3, 3, 0, 3)
    assert counts["dropped_sections"] == 2
    assert all(res["report"]["checks"][k] for k in CHECK_KEYS)
    assert res["summary"].startswith("3 source rows: 3 untouched, 0 amended, 0 unaccounted")
    out = (tmp_path / "o.md").read_bytes()
    assert b"\r" not in out
    text = out.decode("utf-8")
    assert text.count("```yaml plan-tasks") == 1
    assert text.index("## Consolidation delta") < text.index("## Tasks")
    assert not list(tmp_path.glob(".atomic-write.*"))


def test_crlf_sources_report_untouched(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    _write(tmp_path, nl="\r\n")
    res = _run(tmp_path)
    assert res["ok"] is True, res
    assert res["report"]["counts"]["untouched"] == 3
    assert b"\r" not in (tmp_path / "o.md").read_bytes()


def test_carried_section_prefixed_with_origin_label(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    a = _plan([_row("A1")], extra_section="## Notes\n\nkeep this prose\n\n")
    _write(tmp_path, a=a)
    res = _run(tmp_path)
    assert res["ok"] is True, res
    text = (tmp_path / "o.md").read_text(encoding="utf-8")
    assert "## [a] Notes\n\nkeep this prose" in text


def _refused(tmp_path, code):
    res = _run(tmp_path)
    assert res["ok"] is False, res
    assert res["refusal"]["code"] == code, res
    assert not (tmp_path / "o.md").exists()
    return res["refusal"]


def test_undeclared_preamble_refuses_section_unaccounted(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    d = _delta(dropped_sections=[{"heading": "(preamble)", "source": "a.md", "reason": "r"}])
    _write(tmp_path, delta=d)
    _refused(tmp_path, "SECTION_UNACCOUNTED")


def test_phantom_drop_refuses_section_unaccounted(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    d = _delta()
    d["dropped_sections"].append({"heading": "## Nope", "source": "a.md", "reason": "r"})
    _write(tmp_path, delta=d)
    _refused(tmp_path, "SECTION_UNACCOUNTED")


def test_helper_altering_untouched_row_refuses_fidelity_breach(tmp_path, monkeypatch):
    def tamper(rows, d):
        texts = [r.text for r in rows]
        texts[0] = texts[0].replace("work", "tampered")
        return texts, [RowAccount(r.row_id, r.source, "untouched") for r in rows]

    _stub_helpers(monkeypatch, apply_delta=tamper)
    _write(tmp_path)
    _refused(tmp_path, "FIDELITY_BREACH")


def test_amended_row_must_keep_unnamed_keys_and_body_prefix(tmp_path, monkeypatch):
    def amend(rows, d):
        texts = [r.text for r in rows]
        texts[0] = texts[0].replace("surface: x.py", "surface: y.py")
        accts = [RowAccount(r.row_id, r.source, "untouched") for r in rows]
        accts[0] = RowAccount(rows[0].row_id, rows[0].source, "amended", ("fields",))
        return texts, accts

    _stub_helpers(monkeypatch, apply_delta=amend)
    _write(tmp_path)
    _refused(tmp_path, "FIDELITY_BREACH")  # delta names no field edit for A1

    (tmp_path / "d.json").write_text(
        json.dumps(_delta(rows=[{"id": "A1", "fields": {"surface": "y.py"}}])), encoding="utf-8",
    )
    res = _run(tmp_path)
    assert res["ok"] is True, res
    assert res["report"]["counts"]["amended"] == 1


def test_amended_body_must_extend_source_body(tmp_path, monkeypatch):
    def note(rows, d):
        texts = [r.text for r in rows]
        texts[0] = texts[0] + "\n    CONSOLIDATION: n"
        accts = [RowAccount(r.row_id, r.source, "untouched") for r in rows]
        accts[0] = RowAccount(rows[0].row_id, rows[0].source, "amended", ("note",))
        return texts, accts

    _stub_helpers(monkeypatch, apply_delta=note)
    _write(tmp_path)
    assert _run(tmp_path)["ok"] is True

    def rewrite(rows, d):
        texts, accts = note(rows, d)
        texts[0] = texts[0].replace("work", "other")
        return texts, accts

    _stub_helpers(monkeypatch, apply_delta=rewrite)
    res = _handler({"sources": ["a.md", "b.md"], "delta": "d.json", "out": "p.md"}, tmp_path)
    assert res["refusal"]["code"] == "FIDELITY_BREACH"
    assert not (tmp_path / "p.md").exists()


def test_unaccounted_source_row_refuses(tmp_path, monkeypatch):
    def lose(rows, d):
        keep = rows[1:]
        return [r.text for r in keep], [RowAccount(r.row_id, r.source, "untouched") for r in keep]

    _stub_helpers(monkeypatch, apply_delta=lose)
    _write(tmp_path)
    _refused(tmp_path, "FIDELITY_BREACH")


def test_duplicate_ids_refuse_id_collision(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    _write(tmp_path, b=_plan([_row("A1")], title="B"))
    _refused(tmp_path, "ID_COLLISION")


def test_collision_reported_by_helper_refuses(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    sys.modules["coordinator_core.ops._plan_merge_rekey"].detect_collisions = lambda rows: [("A1", ("a.md", "b.md"))]
    _write(tmp_path)
    ref = _refused(tmp_path, "ID_COLLISION")
    assert ref["row_ids"] == ["A1"]


def test_schema_invalid_row_refuses(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    bad = _row("B1").replace("change_kind: code-edit", "change_kind: not-a-kind")
    _write(tmp_path, b=_plan([bad], title="B"))
    _refused(tmp_path, "SCHEMA_INVALID")


def test_differing_required_scalar_without_delta_value_refuses(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    d = _delta()
    del d["frontmatter"]["title"]
    _write(tmp_path, delta=d)
    ref = _refused(tmp_path, "SCHEMA_INVALID")
    assert "'A'" in ref["message"] and "'B'" in ref["message"]


def test_authorization_key_in_delta_refuses(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    d = _delta()
    d["frontmatter"]["execution_authorized_by"] = "PM"
    _write(tmp_path, delta=d)
    _refused(tmp_path, "SCHEMA_INVALID")


def test_dangling_edge_refuses(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    dep = _row("B1", "  depends_on:\n    - chunk: GONE\n      gate_kind: output-consumption-runtime\n      note: n\n")
    _write(tmp_path, b=_plan([dep], title="B"))
    _refused(tmp_path, "DANGLING_EDGE")


@pytest.mark.parametrize("which", ["absent", "two"])
def test_spine_malformed_refuses(tmp_path, monkeypatch, which):
    _stub_helpers(monkeypatch)
    good = _plan([_row("B1")], title="B")
    bad = good.replace("```yaml plan-tasks", "```yaml other") if which == "absent" else good + "\n" + good.split("## Tasks")[1]
    _write(tmp_path, b=bad)
    _refused(tmp_path, "SPINE_MALFORMED")


def test_single_fence_check_runs_on_output(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    _write(tmp_path)
    d = _delta(new_sections=[{"heading": "## Extra", "content": "```yaml plan-tasks\n- id: Z\n```"}])
    (tmp_path / "d.json").write_text(json.dumps(d), encoding="utf-8")
    _refused(tmp_path, "SPINE_MALFORMED")


def test_spine_bearing_prose_is_carried_and_fence_consumed(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    a = _plan([_row("A1")], heading="Open for the PM")
    a = a.replace("## Open for the PM\n\n", "## Open for the PM\n\nquestions here\n\n")
    d = _delta(sources=[{"path": "a.md", "origin_label": "a", "spine_heading": "Open for the PM"}, {"path": "b.md"}])
    _write(tmp_path, a=a, delta=d)
    res = _run(tmp_path)
    assert res["ok"] is True, res
    text = (tmp_path / "o.md").read_text(encoding="utf-8")
    assert "## [a] Open for the PM (prose)\n\nquestions here" in text
    assert text.count("```yaml plan-tasks") == 1


def test_existing_out_replaced_only_with_force_and_only_after_checks(tmp_path, monkeypatch):
    _stub_helpers(monkeypatch)
    _write(tmp_path)
    (tmp_path / "o.md").write_text("keep", encoding="utf-8")
    assert _run(tmp_path)["refusal"]["code"] == "OUTPUT_EXISTS"
    assert _run(tmp_path, force=True)["ok"] is True
    (tmp_path / "o.md").write_text("keep", encoding="utf-8")
    (tmp_path / "d.json").write_text(json.dumps(_delta(dropped_sections=[])), encoding="utf-8")
    assert _run(tmp_path, force=True)["refusal"]["code"] == "SECTION_UNACCOUNTED"
    assert (tmp_path / "o.md").read_text(encoding="utf-8") == "keep"


def test_each_check_flag_is_false_when_its_arm_fails(tmp_path, monkeypatch):
    from coordinator_core.ops import plan_merge

    seen = {}
    real = plan_merge._first_failure

    def spy(checks, details):
        seen.update(checks)
        return real(checks, details)

    monkeypatch.setattr(plan_merge, "_first_failure", spy)
    _stub_helpers(monkeypatch)
    _write(tmp_path)
    assert _run(tmp_path, out="o1.md")["ok"] is True
    assert set(seen) == set(CHECK_KEYS) and all(seen.values())
    (tmp_path / "d.json").write_text(json.dumps(_delta(dropped_sections=[])), encoding="utf-8")
    _run(tmp_path, out="o2.md")
    assert seen["sections_accounted"] is False and seen["single_fence"] is True
