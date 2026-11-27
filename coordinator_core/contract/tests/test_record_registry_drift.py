"""Drift guard: the committed record registry equals what `emit_record_registry` renders."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from coordinator_core.contract import emit_record_registry as err

_KEY = "record-registry.json"


@lru_cache(maxsize=1)
def _rendered() -> dict[str, str]:
    return err.render_all()


def _registry() -> dict:
    return json.loads(_rendered()[_KEY])


def test_committed_file_equals_render() -> None:
    on_disk = err.OUTPUTS[_KEY].read_bytes().decode("utf-8")
    assert on_disk == _rendered()[_KEY], (
        f"{err.OUTPUTS[_KEY]} drifted; run python -m coordinator_core.contract.emit_record_registry"
    )


def test_one_byte_edit_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = _rendered()
    p = tmp_path / _KEY
    p.write_bytes(rendered[_KEY].encode("utf-8"))
    monkeypatch.setattr(err, "OUTPUTS", {_KEY: p})
    assert err.drifted(rendered) == []
    p.write_bytes(rendered[_KEY].encode("utf-8") + b" ")
    assert err.drifted(rendered) == [_KEY]
    p.unlink()
    assert err.drifted(rendered) == [_KEY]


def test_check_mode_exits_nonzero_on_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / _KEY
    p.write_bytes(b"{}\n")
    monkeypatch.setattr(err, "OUTPUTS", {_KEY: p})
    assert err.main(["--check"]) == 1
    assert err.main([]) == 0
    assert err.main(["--check"]) == 0


def test_every_schema_is_a_doc_type_with_globs_fields_order_and_joins() -> None:
    expected = {
        json.loads(f.read_bytes().decode("utf-8"))["x-schema-name"]
        for f in [*err._SCHEMA_DIR.glob("*.schema.json"), *err._MEMO_SCHEMAS]
    }
    doc_types = _registry()["doc_types"]
    assert set(doc_types) == expected | set(err._AUTHORED_TYPES)
    assert {"cross-repo-memo", "archived-memo"} <= set(doc_types)
    vocab = set(err.TYPE_VOCABULARY)
    for name, entry in doc_types.items():
        assert entry["path_globs"], name
        assert entry["fields"], name
        assert set(entry["field_order"]) == set(entry["fields"]), name
        assert isinstance(entry["joins"], list), name
        assert {f["type"] for f in entry["fields"].values()} <= vocab, name


def test_lenient_date_fields_are_typed_date() -> None:
    doc_types = _registry()["doc_types"]
    assert doc_types["plan"]["fields"]["created"]["type"] == "date"
    assert doc_types["decision"]["fields"]["edited"]["type"] == "date"
    assert doc_types["cross-repo-memo"]["fields"]["created"]["type"] == "date"
    assert doc_types["plan"]["fields"]["title"]["type"] == "string"
    assert doc_types["plan"]["fields"]["deliverable_id"]["nullable"] is True


def test_field_order_is_schema_declaration_order() -> None:
    schema = json.loads(
        (err._SCHEMA_DIR / "plan.schema.json").read_bytes().decode("utf-8")
    )
    assert _registry()["doc_types"]["plan"]["field_order"] == list(schema["properties"])


def test_required_deliverable_and_decision_join_edges() -> None:
    doc_types = _registry()["doc_types"]
    assert {
        "from_field": "deliverable_id",
        "to_doc_type": "handoff",
        "to_field": "deliverable_id",
        "match": "equals",
    } in doc_types["plan"]["joins"]
    for citer in ("plan", "handoff"):
        assert {
            "from_field": "$body",
            "to_doc_type": "decision",
            "to_field": "id",
            "match": "body_citation",
            "pattern": err._DECISION_PATTERN,
        } in doc_types[citer]["joins"]


def test_every_join_resolves_to_real_doc_types_and_fields() -> None:
    doc_types = _registry()["doc_types"]
    for name, entry in doc_types.items():
        for j in entry["joins"]:
            assert j["match"] in err.MATCH_VOCABULARY
            assert j["from_field"] == "$body" or j["from_field"] in entry["fields"], (name, j)
            assert j["to_doc_type"] in doc_types, (name, j)
            if "to_field" in j:
                assert j["to_field"] in doc_types[j["to_doc_type"]]["fields"], (name, j)


_DR_PATTERN = r"\bDR-[A-Za-z0-9][A-Za-z0-9-]*\b"


def _joins(doc_type: str) -> list[dict]:
    return _registry()["doc_types"][doc_type]["joins"]


def test_decision_globs_and_supersession_join_by_id() -> None:
    decision = _registry()["doc_types"]["decision"]
    assert decision["path_globs"] == ["docs/decisions/*.md", "coordinator/docs/decisions/*.md"]
    for field in ("superseded_by", "supersedes"):
        assert field in decision["fields"]
        assert {
            "from_field": field,
            "to_doc_type": "decision",
            "to_field": "id",
            "match": "equals",
            "skip_values": ["none"],
        } in decision["joins"]
    assert not [j for j in decision["joins"] if j["match"] == "path"]


def test_dr_body_citation_pattern_is_widened_and_matches_non_numeric_ids() -> None:
    import re

    assert err._DECISION_PATTERN == _DR_PATTERN
    hits = re.findall(_DR_PATTERN, "DR-12 DR-PV1-003 DR-pm-guiding-objectives DR-")
    assert hits == ["DR-12", "DR-PV1-003", "DR-pm-guiding-objectives"]
    for citer in ("plan", "handoff", "handoff-archived", "completion-entry", "wiki-page", "tripwire"):
        assert {
            "from_field": "$body",
            "to_doc_type": "decision",
            "to_field": "id",
            "match": "body_citation",
            "pattern": _DR_PATTERN,
        } in _joins(citer), citer


def test_plan_depends_on_plan_joins_plan_by_path_of_item_plan() -> None:
    assert {
        "from_field": "depends_on_plan",
        "to_doc_type": "plan",
        "match": "path",
        "item_field": "plan",
    } in _joins("plan")


def test_lesson_entry_covers_the_archive_glob() -> None:
    globs = _registry()["doc_types"]["lesson-entry"]["path_globs"]
    assert "state/lessons/*.yaml" in globs
    assert "archive/lessons/*/*.yaml" in globs


@pytest.mark.parametrize("queue", ["bug-backlog", "debt-backlog", "improvement-queue"])
def test_queue_entries_cover_the_archive_glob(queue: str) -> None:
    globs = _registry()["doc_types"][queue]["path_globs"]
    assert globs == [f"state/{queue}/*.yaml", f"archive/{queue}/*/*.yaml"]


def test_research_synthesis_globs_only_readme_and_synthesis() -> None:
    globs = _registry()["doc_types"]["research-synthesis"]["path_globs"]
    assert globs == ["docs/research/**/README.md", "docs/research/**/synthesis.md"]


def test_wiki_page_tripwire_and_audit_types() -> None:
    doc_types = _registry()["doc_types"]
    wiki, trip, audit = doc_types["wiki-page"], doc_types["tripwire"], doc_types["audit"]
    assert wiki["path_globs"] == ["coordinator/docs/wiki/**/*.md", "docs/wiki/**/*.md"]
    assert "coordinator/docs/wiki/coordinator-tripwires/**" in wiki["exclude_globs"]
    assert "**/_index.md" in wiki["exclude_globs"]
    assert wiki["identity"] == {"from": "path"}
    assert not [f for f in wiki["fields"].values() if f.get("required")]
    assert wiki["fields"]["title"]["derive"] == {"from": "first_h1"}
    assert wiki["fields"]["summary"]["derive"] == {"from": "first_sentence_after_h1"}
    assert trip["path_globs"] == [
        "coordinator/docs/wiki/coordinator-tripwires/*.md",
        "coordinator/docs/wiki/coordinator-tripwires/tripwire-registry/*.md",
    ]
    assert set(trip["exclude_globs"]) == {"**/README.md", "**/_index.md"}
    assert trip["identity"] == {"from": "field", "field": "token"}
    assert trip["fields"]["token"]["derive"] == {"from": "h2"}
    assert {k: trip["fields"][k]["derive"]["label"] for k in ("tell", "rule", "source")} == {
        "tell": "**Tell:**", "rule": "**Rule:**", "source": "Source:",
    }
    kind = trip["fields"]["kind"]
    assert kind["enum"] == ["behavioural", "mechanism"]
    assert kind["derive"]["default"] == "behavioural"
    assert kind["derive"]["prefixes"] == {
        "coordinator/docs/wiki/coordinator-tripwires/tripwire-registry/": "mechanism"
    }
    assert audit["path_globs"] == ["state/audits/*.md", "state/audits/*/README.md"]
    assert audit["identity"] == {"from": "path"}
    assert set(audit["fields"]) == {"title"}
    for src in ("wiki-page", "tripwire"):
        for dst in ("wiki-page", "tripwire"):
            assert {
                "from_field": "$body", "to_doc_type": dst, "match": "body_link",
                "pattern": err._LINK_PATTERN,
            } in _joins(src), (src, dst)


def test_body_link_pattern_captures_the_md_target() -> None:
    import re

    text = "see [a](foo/bar.md) and [b](baz.md#anchor) and [c](https://x.io/p) and [d](img.png)"
    assert re.findall(err._LINK_PATTERN, text) == ["foo/bar.md", "baz.md"]


def test_globs_are_repo_relative_posix_with_no_drive_or_home() -> None:
    registry = _registry()
    assert registry["glob_root"] == "repo-root"
    for name, entry in registry["doc_types"].items():
        for g in [*entry["path_globs"], *entry.get("exclude_globs", [])]:
            assert "\\" not in g and not g.startswith("/") and ":" not in g, (name, g)


_EXTENSION = {
    "extend_globs": {"audit": ["reports/audits/*.md"]},
    "doc_types": {
        "runbook": {
            "path_globs": ["ops/runbooks/*.md"],
            "path_globs_source": "repo-declared",
            "identity": {"from": "path"},
            "fields": {"title": {"type": "string", "derive": {"from": "first_h1"}}},
        }
    },
    "joins": [["runbook", "$body", "decision", "id", "body_citation"]],
}


def test_repo_extension_merges_over_the_shared_registry() -> None:
    shared = _registry()
    merged = json.loads(err.render_all(_EXTENSION)[_KEY])["doc_types"]
    assert set(merged) == set(shared["doc_types"]) | {"runbook"}
    assert merged["audit"]["path_globs"] == [*shared["doc_types"]["audit"]["path_globs"], "reports/audits/*.md"]
    assert merged["runbook"]["path_globs"] == ["ops/runbooks/*.md"]
    assert {
        "from_field": "$body", "to_doc_type": "decision", "to_field": "id",
        "match": "body_citation", "pattern": _DR_PATTERN,
    } in merged["runbook"]["joins"]
    assert merged["plan"] == shared["doc_types"]["plan"]


_FIXTURE_REPO = Path(__file__).parent / "fixtures" / "record_registry_repo"


def test_resolve_registry_merges_the_fixture_repo_extension_over_the_shared_registry() -> None:
    shared = _registry()["doc_types"]
    resolved = err.resolve_registry(_FIXTURE_REPO)["doc_types"]
    assert set(resolved) == set(shared) | {"runbook"}
    assert resolved["runbook"]["path_globs"] == ["ops/runbooks/*.md"]
    assert resolved["runbook"]["joins"] == [{
        "from_field": "$body", "to_doc_type": "decision", "to_field": "id",
        "match": "body_citation", "pattern": _DR_PATTERN,
    }]
    assert all(resolved[name] == shared[name] for name in shared)


def test_resolve_registry_without_an_extension_file_is_the_shared_registry(tmp_path: Path) -> None:
    assert err.resolve_registry(tmp_path) == _registry()


def test_resolve_registry_refuses_a_redeclaring_extension(tmp_path: Path) -> None:
    ext = tmp_path / err.EXTENSION_HOME
    ext.parent.mkdir(parents=True)
    ext.write_text(json.dumps({"doc_types": {"plan": _EXTENSION["doc_types"]["runbook"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="redeclares"):
        err.resolve_registry(tmp_path)


def test_repo_extension_cannot_redeclare_a_shared_type_or_extend_an_unknown_one() -> None:
    with pytest.raises(ValueError, match="redeclares"):
        err.render_all({"doc_types": {"plan": _EXTENSION["doc_types"]["runbook"]}})
    with pytest.raises(ValueError, match="unknown doc_type"):
        err.render_all({"extend_globs": {"nope": ["x/*.md"]}})
    bad = {"doc_types": {"x": {**_EXTENSION["doc_types"]["runbook"], "fields": {"f": {"type": "bogus"}}}}}
    with pytest.raises(ValueError, match="type vocabulary"):
        err.render_all(bad)


def test_cli_extension_writes_only_to_the_named_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared_out = tmp_path / "shared" / _KEY
    monkeypatch.setattr(err, "OUTPUTS", {_KEY: shared_out})
    ext = tmp_path / "ext.json"
    ext.write_text(json.dumps(_EXTENSION), encoding="utf-8")
    out = tmp_path / "repo" / _KEY
    with pytest.raises(SystemExit):
        err.main(["--extension", str(ext)])
    assert err.main(["--extension", str(ext), "--output", str(out)]) == 0
    assert "runbook" in json.loads(out.read_bytes().decode("utf-8"))["doc_types"]
    assert not shared_out.exists()
    assert err.main(["--extension", str(ext), "--output", str(out), "--check"]) == 0
    out.write_bytes(out.read_bytes() + b" ")
    assert err.main(["--extension", str(ext), "--output", str(out), "--check"]) == 1


def test_schema_with_no_path_mapping_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orphan = tmp_path / "orphan.schema.json"
    orphan.write_text(
        json.dumps({"x-schema-name": "orphan-record", "type": "object", "properties": {"a": {"type": "string"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(err, "_MEMO_SCHEMAS", (*err._MEMO_SCHEMAS, orphan))
    with pytest.raises(ValueError, match="orphan-record"):
        err.render_all()


def test_join_to_unknown_field_is_refused() -> None:
    doc_types = {
        "a": {"fields": {"x": {}}, "joins": []},
        "b": {"fields": {"y": {}}, "joins": []},
    }
    with pytest.raises(ValueError, match="no such field"):
        err._attach_joins(doc_types, [("a", "missing", "b", "y", "equals")])
    with pytest.raises(ValueError, match="no such field"):
        err._attach_joins(doc_types, [("a", "x", "b", "nope", "equals")])
    with pytest.raises(ValueError, match="unknown doc_type"):
        err._attach_joins(doc_types, [("a", "x", "zzz", "y", "equals")])
