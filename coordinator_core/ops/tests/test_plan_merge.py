"""plan.merge end to end through the registry: the pinned worked example plus every refusal arm.

Everything runs over `ipc.dispatch_message` in `tmp_path`; fixtures are copied in at the delta's
own `docs/plans/...` paths. Fixture provenance and the AC map: fixtures/plan_merge/README.md.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest

from coordinator_core import ipc

FIX = Path(__file__).parent / "fixtures" / "plan_merge"
DELTA_FIXTURE = FIX / "worked-example.delta.json"
SCHEMA_DIR_PARAM = "schema_dir"
SRC_MERGED = "docs/plans/merged-execution-shape.md"
SRC_SEAM = "docs/plans/typed-alternative-record-seam.md"
OUT = "docs/plans/out.md"
CHECKS = {"row_partition", "single_fence", "unique_ids", "schema_valid", "sections_accounted", "edges_resolve"}


def _call(root: Path, params: dict) -> dict:
    (root / ".git").mkdir(exist_ok=True)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "plan.merge", "params": params, "_origin_worktree": str(root)}
    return asyncio.run(ipc.dispatch_message(msg))


def _result(root: Path, params: dict) -> dict:
    d = _call(root, params)
    assert "error" not in d, d
    return d["result"]


def _refused(root: Path, params: dict, code: str) -> dict:
    r = _result(root, params)
    assert r["ok"] is False, r
    assert r["refusal"]["code"] == code, r["refusal"]
    assert not (root / params["out"]).exists()
    return r["refusal"]


def _put(root: Path, rel: str, text: str, nl: str = "\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", nl).encode("utf-8"))


def _dep(*chunks: str) -> str:
    return "  depends_on:\n" + "".join(
        f"    - chunk: {c}\n      gate_kind: output-consumption-runtime\n      note: n\n" for c in chunks
    )


def _row(rid: str, extra: str = "", body: str = "work") -> str:
    return (
        f'- id: {rid}\n  title: "t {rid}"\n  change_kind: code-edit\n  surface: x.py\n  writes: []\n'
        f"{extra}  body: |\n    {body}\n"
    )


def _plan(rows: list, heading: str = "Tasks", title: str = "A", fence: bool = True, second_fence: bool = False) -> str:
    spine = "```yaml plan-tasks\n" + "".join(rows) + "```\n"
    parts = [f'---\ntitle: "{title}"\ncreated: "2026-10-22"\nauthor: EM\nstatus: draft\n---\n\n# {title}\n\nintro\n\n']
    if fence:
        parts.append(f"## {heading}\n\n{spine}")
    if second_fence:
        parts.append(f"\n## Again\n\n{spine}")
    return "".join(parts)


def _delta(**over) -> dict:
    d = {
        "sources": [
            {"path": "docs/plans/a.md", "origin_label": "a"},
            {"path": "docs/plans/b.md", "origin_label": "b"},
        ],
        "frontmatter": {"title": "M", "created": "2026-10-22", "author": "EM", "status": "draft"},
        "dropped_sections": [
            {"heading": "(preamble)", "source": "docs/plans/a.md", "reason": "superseded"},
            {"heading": "(preamble)", "source": "docs/plans/b.md", "reason": "superseded"},
        ],
    }
    d.update(over)
    return d


def _synth(root: Path, a_rows: list, b_rows: list, delta: dict | None = None, **plan_kw) -> dict:
    _put(root, "docs/plans/a.md", _plan(a_rows, title="A", **plan_kw))
    _put(root, "docs/plans/b.md", _plan(b_rows, title="B"))
    _put(root, "docs/plans/d.json", json.dumps(delta if delta is not None else _delta()))
    return {"sources": ["docs/plans/a.md", "docs/plans/b.md"], "delta": "docs/plans/d.json", "out": OUT}


def _stage_worked(root: Path, delta: dict | None = None, nl: str = "\n") -> dict:
    for name, rel in (("merged-execution-shape.md", SRC_MERGED), ("typed-alternative-record-seam.md", SRC_SEAM)):
        text = (FIX / name).read_bytes().decode("utf-8").replace("\r\n", "\n")
        _put(root, rel, text, nl)
    d = delta if delta is not None else json.loads(DELTA_FIXTURE.read_text(encoding="utf-8"))
    _put(root, "docs/plans/worked.delta.json", json.dumps(d))
    shutil.copytree(FIX / "schemas", root / "schemas")
    return {
        "sources": [SRC_MERGED, SRC_SEAM],
        "delta": "docs/plans/worked.delta.json",
        "out": OUT,
        SCHEMA_DIR_PARAM: "schemas",
    }


def _worked_delta() -> dict:
    return json.loads(DELTA_FIXTURE.read_text(encoding="utf-8"))


def _spine_ids(text: str) -> list:
    return re.findall(r"(?m)^- id:[ \t]*(\S+)", text.split("## Tasks", 1)[1])


class TestWorkedExample:
    def test_partition_counts_and_checks(self, tmp_path):
        params = _stage_worked(tmp_path)
        r = _result(tmp_path, params)
        assert r["ok"] is True, r
        rep = r["report"]
        assert rep["counts"] == {
            "source_rows": 23, "untouched": 9, "amended": 14, "unaccounted": 0,
            "new": 1, "out": 24, "dropped_sections": 4,
        }
        assert set(rep["checks"]) == CHECKS and all(rep["checks"].values())
        assert (tmp_path / OUT).is_file()

    def test_output_row_order_matches_pinned_consolidation(self, tmp_path):
        r = _result(tmp_path, _stage_worked(tmp_path))
        assert r["ok"] is True, r
        out_ids = _spine_ids((tmp_path / OUT).read_text(encoding="utf-8"))
        expected_ids = _spine_ids((FIX / "expected-consolidated.md").read_text(encoding="utf-8"))
        assert out_ids == expected_ids
        assert len(out_ids) == 24

    def test_amended_values_match_pinned_consolidation(self, tmp_path):
        r = _result(tmp_path, _stage_worked(tmp_path))
        assert r["ok"] is True, r
        out = (tmp_path / OUT).read_text(encoding="utf-8")
        expected = (FIX / "expected-consolidated.md").read_text(encoding="utf-8")
        for needle in (
            "disposition_ref: 061134b1",
            "id: S3C",
            "id: M14S",
        ):
            assert needle in expected, needle
            assert needle in out, needle

    def test_four_declared_drops_reported(self, tmp_path):
        r = _result(tmp_path, _stage_worked(tmp_path))
        assert r["ok"] is True, r
        assert len(r["report"]["dropped"]) == 4

    def test_sources_are_never_modified(self, tmp_path):
        params = _stage_worked(tmp_path)
        before = {s: (tmp_path / s).read_bytes() for s in params["sources"]}
        _result(tmp_path, params)
        assert {s: (tmp_path / s).read_bytes() for s in params["sources"]} == before

    def test_crlf_sources_give_identical_counts(self, tmp_path):
        lf = tmp_path / "lf"
        crlf = tmp_path / "crlf"
        lf.mkdir()
        crlf.mkdir()
        a = _result(lf, _stage_worked(lf))
        b = _result(crlf, _stage_worked(crlf, nl="\r\n"))
        assert a["ok"] and b["ok"], (a, b)
        assert a["report"]["counts"] == b["report"]["counts"]
        assert (lf / OUT).read_bytes() == (crlf / OUT).read_bytes()

    def test_worked_example_passes_the_live_vendored_schema(self, tmp_path, record_property):
        from coordinator_core.frontmatter import schema_validate as sv

        live = Path(sv.__file__).parent / "schemas" / "plan-tasks.schema.json"
        version = json.loads(live.read_text(encoding="utf-8"))["x-schema-version"]
        record_property("live_plan_tasks_schema_version", version)
        params = _stage_worked(tmp_path)
        del params[SCHEMA_DIR_PARAM]
        r = _result(tmp_path, params)
        assert r["ok"] is True, (version, r)
        assert all(r["report"]["checks"].values()), version


class TestRefusals:
    def test_id_collision(self, tmp_path):
        params = _synth(tmp_path, [_row("C1"), _row("C2"), _row("C3")], [_row("C1"), _row("C2"), _row("C3")])
        ref = _refused(tmp_path, params, "ID_COLLISION")
        assert set(ref["row_ids"]) == {"C1", "C2", "C3"}

    def test_rekey_resolves_collision_and_rewrites_edge(self, tmp_path):
        a = [_row("C1"), _row("C2", _dep("C1"))]
        b = [_row("C1"), _row("C2", _dep("C1"))]
        d = _delta(rekey=[{"source": "b", "ids": {"C1": "B1", "C2": "B2"}}])
        params = _synth(tmp_path, a, b, d)
        r = _result(tmp_path, params)
        assert r["ok"] is True, r
        out = (tmp_path / OUT).read_text(encoding="utf-8")
        assert _spine_ids(out) == ["C1", "C2", "B1", "B2"]
        assert re.search(r"(?m)^- id: B2\n(?:.*\n)*?  depends_on:\n    - chunk: B1\n", out)
        classes = {row["row_id"]: row["edit_classes"] for row in r["report"]["rows"]}
        assert "rekey" in classes["B1"] and "rekey" in classes["B2"]
        assert classes["C1"] == []

    def test_rekey_dangling(self, tmp_path):
        d = _delta(rekey=[{"source": "no-such-source", "ids": {"C1": "B1"}}])
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        _refused(tmp_path, params, "REKEY_DANGLING")

    def test_rekey_of_absent_id_is_dangling(self, tmp_path):
        d = _delta(rekey=[{"source": "b", "ids": {"NOPE": "B9"}}])
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        _refused(tmp_path, params, "REKEY_DANGLING")

    def test_delta_unmatched_row(self, tmp_path):
        d = _delta(rows=[{"id": "GHOST", "fields": {"title": "x"}}])
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        ref = _refused(tmp_path, params, "DELTA_UNMATCHED_ROW")
        assert "GHOST" in ref["row_ids"]

    def test_delta_unmatched_anchor(self, tmp_path):
        new = {
            "id": "N1", "new": True, "after": "NOWHERE", "body": "b",
            "fields": {"title": "n", "change_kind": "code-edit", "surface": "x.py"},
        }
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], _delta(rows=[new]))
        _refused(tmp_path, params, "DELTA_UNMATCHED_ANCHOR")

    def test_new_row_with_valid_anchor_is_accounted_new(self, tmp_path):
        new = {
            "id": "N1", "new": True, "after": "A1", "body": "b",
            "fields": {"title": "n", "change_kind": "code-edit", "surface": "x.py", "writes": []},
        }
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], _delta(rows=[new]))
        r = _result(tmp_path, params)
        assert r["ok"] is True, r
        assert r["report"]["counts"]["new"] == 1
        assert _spine_ids((tmp_path / OUT).read_text(encoding="utf-8")) == ["A1", "N1", "B1"]

    def test_undeclared_prose_section_is_carried_not_dropped(self, tmp_path):
        params = _synth(tmp_path, [_row("A1")], [_row("B1")])
        _put(
            tmp_path, "docs/plans/a.md",
            _plan([_row("A1")], title="A").replace("## Tasks", "## Orphan prose\n\ntext\n\n## Tasks"),
        )
        r = _result(tmp_path, params)
        assert r["ok"] is True, r
        assert "text" in (tmp_path / OUT).read_text(encoding="utf-8")

    def test_section_unaccounted_worked_delta_without_preamble_drops(self, tmp_path):
        d = _worked_delta()
        d["dropped_sections"] = [x for x in d["dropped_sections"] if x["heading"] != "(preamble)"]
        assert len(d["dropped_sections"]) == 2
        params = _stage_worked(tmp_path, d)
        _refused(tmp_path, params, "SECTION_UNACCOUNTED")

    def test_section_unaccounted_for_a_declared_drop_matching_nothing(self, tmp_path):
        extra = {"heading": "## Not a real section", "source": "docs/plans/a.md", "reason": "r"}
        d = _delta()
        d["dropped_sections"].append(extra)
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        _refused(tmp_path, params, "SECTION_UNACCOUNTED")

    def test_schema_invalid_out_of_enum_disposition(self, tmp_path):
        d = _delta(rows=[{"id": "A1", "fields": {"disposition": "finished"}}])
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        _refused(tmp_path, params, "SCHEMA_INVALID")

    def test_schema_invalid_delta_asserting_approved(self, tmp_path):
        d = _delta()
        d["frontmatter"]["status"] = "approved"
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        _refused(tmp_path, params, "SCHEMA_INVALID")

    def test_schema_invalid_delta_carrying_an_authorization_key(self, tmp_path):
        d = _delta()
        d["frontmatter"]["execution_authorized_sha"] = "abc1234"
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        _refused(tmp_path, params, "SCHEMA_INVALID")

    def test_dangling_edge(self, tmp_path):
        a = [_row("A1", _dep("A9"))]
        params = _synth(tmp_path, a, [_row("B1")])
        ref = _refused(tmp_path, params, "DANGLING_EDGE")
        assert "A9" in json.dumps(ref)

    def test_spine_malformed_no_fence(self, tmp_path):
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], fence=False)
        ref = _refused(tmp_path, params, "SPINE_MALFORMED")
        assert "docs/plans/a.md" in ref["sources"]

    def test_spine_malformed_two_fences(self, tmp_path):
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], second_fence=True)
        _refused(tmp_path, params, "SPINE_MALFORMED")

    def test_spine_malformed_when_spine_heading_is_not_given(self, tmp_path):
        d = _worked_delta()
        d["sources"][0].pop("spine_heading")
        params = _stage_worked(tmp_path, d)
        ref = _refused(tmp_path, params, "SPINE_MALFORMED")
        assert SRC_MERGED in ref["sources"]

    def test_field_edit_is_accounted_amended(self, tmp_path):
        d = _delta(rows=[{"id": "A1", "fields": {"title": "changed"}}])
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        r = _result(tmp_path, params)
        assert r["ok"] is True, r
        counts = r["report"]["counts"]
        assert counts["amended"] == 1 and counts["untouched"] == 1 and counts["unaccounted"] == 0
        assert 'title: "changed"' in (tmp_path / OUT).read_text(encoding="utf-8") or "title: changed" in (
            tmp_path / OUT
        ).read_text(encoding="utf-8")


class TestOutputGuards:
    def test_output_is_source_refused_with_and_without_force(self, tmp_path):
        params = _synth(tmp_path, [_row("A1")], [_row("B1")])
        before = (tmp_path / "docs/plans/a.md").read_bytes()
        for force in (False, True):
            p = {**params, "out": "docs/plans/a.md", "force": force}
            r = _result(tmp_path, p)
            assert r["ok"] is False and r["refusal"]["code"] == "OUTPUT_IS_SOURCE", r
            assert (tmp_path / "docs/plans/a.md").read_bytes() == before

    def test_output_exists_refused_without_force_and_replaced_with_force(self, tmp_path):
        params = _synth(tmp_path, [_row("A1")], [_row("B1")])
        _put(tmp_path, OUT, "sentinel\n")
        r = _result(tmp_path, params)
        assert r["ok"] is False and r["refusal"]["code"] == "OUTPUT_EXISTS", r
        assert (tmp_path / OUT).read_text(encoding="utf-8") == "sentinel\n"
        r = _result(tmp_path, {**params, "force": True})
        assert r["ok"] is True, r
        assert "sentinel" not in (tmp_path / OUT).read_text(encoding="utf-8")

    def test_refusal_leaves_no_temp_file_beside_out(self, tmp_path):
        params = _synth(tmp_path, [_row("A1", _dep("A9"))], [_row("B1")])
        _refused(tmp_path, params, "DANGLING_EDGE")
        assert sorted(p.name for p in (tmp_path / "docs/plans").iterdir()) == ["a.md", "b.md", "d.json"]


class TestTopLevelRowEdits:
    def test_deferred_entry_passes_fidelity_and_stops_at_the_schema_enum(self, tmp_path):
        # `deferred: true` maps to `disposition: deferred`, which the closed live enum refuses; the
        # refusal must come from schema_valid, not from the row-partition check.
        d = _delta(rows=[{"id": "A1", "deferred": True}])
        params = _synth(tmp_path, [_row("A1")], [_row("B1")], d)
        ref = _refused(tmp_path, params, "SCHEMA_INVALID")
        assert "disposition" in ref["message"], ref

    def test_note_on_row_without_block_body_adds_consolidation_key(self, tmp_path):
        a = "- id: A1\n  title: t\n  change_kind: code-edit\n  surface: x.py\n  writes: []\n"
        d = _delta(rows=[{"id": "A1", "note": "folded in"}])
        r = _result(tmp_path, _synth(tmp_path, [a], [_row("B1")], d))
        assert r["ok"] is True, r
        assert "consolidation: |" in (tmp_path / OUT).read_text(encoding="utf-8")


def test_every_refusal_code_is_pinned_by_a_quoted_code_in_a_plan_merge_test():
    from coordinator_core.ops._plan_merge_contract import REFUSAL_CODES

    here = Path(__file__).parent
    quoted = set()
    for name in ("test_plan_merge.py", "test_plan_merge_verify.py"):
        quoted |= set(re.findall(r"""["']([A-Z_]{4,})["']""", (here / name).read_text(encoding="utf-8")))
    assert set(REFUSAL_CODES) - quoted == set()


def test_schema_dir_missing_a_schema_file_raises_value_error(tmp_path):
    params = _stage_worked(tmp_path)
    (tmp_path / "schemas" / "plan.schema.json").unlink()
    d = _call(tmp_path, params)
    assert "cannot read schema" in json.dumps(d), d
