
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import queue_family
from coordinator_core.ops.dispatch_emit.queue_select import (
    DuplicateStemError,
    Manifest,
    MissingRowIdError,
    RouteToRefusedError,
    WhereTermError,
    select_rows,
)


def _write_row(path: Path, **fields) -> None:
    lines = []
    for key, value in fields.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(json.dumps(v) for v in value) + "]"
            lines.append(f"{key}: {rendered}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _base_row(**overrides) -> dict:
    row = dict(
        created="2026-09-21",
        title="a bug row",
        body="a body",
        status="open",
        surface="coordinator_core/x",
        severity="P2",
    )
    row.update(overrides)
    return row


def _select(**kwargs):
    kwargs.setdefault("where", None)
    kwargs.setdefault("order", None)
    kwargs.setdefault("limit", None)
    kwargs.setdefault("batch_key", ["severity"])
    kwargs.setdefault("batch_sizes", {"P0": 2, "P1": 6, "P2": 10, "P3": 12, "@unkeyed": 4})
    kwargs.setdefault("row_id_key", "@stem")
    kwargs.setdefault("profile", "fixture")
    return select_rows(**kwargs)


def test_operator_eq(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(severity="P0"))
    _write_row(queue_dir / "b.yaml", **_base_row(severity="P1"))
    manifest = _select(
        queue=[queue_dir], repo_root=tmp_path, where=[[["severity", "==", "P0"]]]
    )
    assert [e.row_id for e in manifest.entries] == ["a"]


def test_operator_in(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(severity="P0"))
    _write_row(queue_dir / "b.yaml", **_base_row(severity="P1"))
    _write_row(queue_dir / "c.yaml", **_base_row(severity="P3"))
    manifest = _select(
        queue=[queue_dir], repo_root=tmp_path, where=[[["severity", "in", ["P0", "P1"]]]]
    )
    assert {e.row_id for e in manifest.entries} == {"a", "b"}


def test_operator_present(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(tags=["x"]))
    _write_row(queue_dir / "b.yaml", **_base_row())
    manifest = _select(queue=[queue_dir], repo_root=tmp_path, where=[[["tags", "present"]]])
    assert [e.row_id for e in manifest.entries] == ["a"]


def test_operator_absent(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(tags=["x"]))
    _write_row(queue_dir / "b.yaml", **_base_row())
    manifest = _select(queue=[queue_dir], repo_root=tmp_path, where=[[["tags", "absent"]]])
    assert [e.row_id for e in manifest.entries] == ["b"]


def test_operator_contains(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(tags=["needs-judgment"]))
    _write_row(queue_dir / "b.yaml", **_base_row(tags=["other"]))
    manifest = _select(
        queue=[queue_dir], repo_root=tmp_path, where=[[["tags", "contains", "needs-judgment"]]]
    )
    assert [e.row_id for e in manifest.entries] == ["a"]


def test_operator_contains_refuses_non_list_field(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row())
    with pytest.raises(WhereTermError):
        _select(queue=[queue_dir], repo_root=tmp_path, where=[[["severity", "contains", "P0"]]])


def test_unsupported_operator_refused(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row())
    with pytest.raises(WhereTermError):
        _select(queue=[queue_dir], repo_root=tmp_path, where=[[["severity", "!=", "P0"]]])


def test_sentinel_literal_in_eq_refused(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row())
    with pytest.raises(WhereTermError):
        _select(
            queue=[queue_dir],
            repo_root=tmp_path,
            where=[[["severity", "==", "unset"]]],
            absent_sentinels={"severity": ["unset"]},
        )


def test_sentinel_literal_in_in_refused(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row())
    with pytest.raises(WhereTermError):
        _select(
            queue=[queue_dir],
            repo_root=tmp_path,
            where=[[["severity", "in", ["P0", "unset"]]]],
            absent_sentinels={"severity": ["unset"]},
        )


def test_duplicate_stem_refused(tmp_path):
    dir_a = tmp_path / "state" / "bug-backlog"
    dir_b = tmp_path / "state" / "scratch-tf"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    _write_row(dir_a / "row-1.yaml", **_base_row())
    _write_row(dir_b / "row-1.yaml", **_base_row())
    with pytest.raises(DuplicateStemError):
        _select(queue=[dir_a, dir_b], repo_root=tmp_path)


def test_non_yaml_files_ignored(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    (queue_dir / ".gitkeep").write_text("", encoding="utf-8")
    (queue_dir / "README.md").write_text("not a row", encoding="utf-8")
    _write_row(queue_dir / "row-1.yaml", **_base_row())
    _write_row(queue_dir / "row-2.yml", **_base_row())
    manifest = _select(queue=[queue_dir], repo_root=tmp_path)
    assert len(manifest.entries) == 2
    assert {e.row_id for e in manifest.entries} == {"row-1", "row-2"}


def test_source_record_missing_row_id_field_refused(tmp_path, monkeypatch):
    from coordinator_core.contract import grind_vocab as _grind_vocab
    from coordinator_core.ops.dispatch_emit import queue_select as _qs

    monkeypatch.setattr(_grind_vocab, "SOURCE_OPS", frozenset({"fake.source"}))

    def _fake_call_source_op(op_name, args, repo_root):
        return {"records": [{"title": "no id field here"}]}

    monkeypatch.setattr(_qs, "_call_source_op", _fake_call_source_op)

    with pytest.raises(MissingRowIdError):
        _select(
            queue=[],
            repo_root=tmp_path,
            row_id_key="row_id",
            source={"op": "fake.source", "args": {}},
        )


def test_source_record_at_stem_derives_stable_content_id(tmp_path, monkeypatch):
    from coordinator_core.contract import grind_vocab as _grind_vocab
    from coordinator_core.ops.dispatch_emit import queue_select as _qs

    monkeypatch.setattr(_grind_vocab, "SOURCE_OPS", frozenset({"fake.source"}))

    records = [{"title": "first"}, {"title": "second"}]

    def _fake_call_source_op(op_name, args, repo_root):
        return {"records": records}

    monkeypatch.setattr(_qs, "_call_source_op", _fake_call_source_op)

    manifest = _select(
        queue=[], repo_root=tmp_path, row_id_key="@stem", source={"op": "fake.source", "args": {}}
    )
    ids_forward = [e.row_id for e in manifest.entries]

    def _fake_call_source_op_reordered(op_name, args, repo_root):
        return {"records": list(reversed(records))}

    monkeypatch.setattr(_qs, "_call_source_op", _fake_call_source_op_reordered)
    manifest_reordered = _select(
        queue=[], repo_root=tmp_path, row_id_key="@stem", source={"op": "fake.source", "args": {}}
    )
    ids_reversed = [e.row_id for e in manifest_reordered.entries]

    assert set(ids_forward) == set(ids_reversed)
    assert len(set(ids_forward)) == 2


def test_source_record_at_stem_duplicate_content_refused(tmp_path, monkeypatch):
    from coordinator_core.contract import grind_vocab as _grind_vocab
    from coordinator_core.ops.dispatch_emit import queue_select as _qs

    monkeypatch.setattr(_grind_vocab, "SOURCE_OPS", frozenset({"fake.source"}))

    def _fake_call_source_op(op_name, args, repo_root):
        return {"records": [{"title": "same"}, {"title": "same"}]}

    monkeypatch.setattr(_qs, "_call_source_op", _fake_call_source_op)

    with pytest.raises(DuplicateStemError):
        _select(
            queue=[],
            repo_root=tmp_path,
            row_id_key="@stem",
            source={"op": "fake.source", "args": {}},
        )


def test_unparseable_row_raises_named_error(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    bad = queue_dir / "bad.yaml"
    bad.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(Exception):
        _select(queue=[queue_dir], repo_root=tmp_path)


def test_sentinel_normalisation_feeds_where_and_batch_key(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(severity="unset"))
    manifest = _select(
        queue=[queue_dir],
        repo_root=tmp_path,
        where=[[["severity", "absent"]]],
        absent_sentinels={"severity": ["unset"]},
    )
    assert len(manifest.entries) == 1
    assert manifest.entries[0].batch_key == "@unkeyed"


def test_unkeyed_batch_for_missing_every_key(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    row = _base_row()
    del row["severity"]
    _write_row(queue_dir / "a.yaml", **row)
    manifest = _select(queue=[queue_dir], repo_root=tmp_path)
    assert manifest.entries[0].batch_key == "@unkeyed"


def test_ordering_by_priority_field(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "low.yaml", **_base_row(severity="P3"))
    _write_row(queue_dir / "high.yaml", **_base_row(severity="P0"))
    _write_row(queue_dir / "mid.yaml", **_base_row(severity="P1"))
    manifest = _select(
        queue=[queue_dir], repo_root=tmp_path, order=("severity", ["P0", "P1", "P2", "P3"])
    )
    assert [e.row_id for e in manifest.entries] == ["high", "mid", "low"]


def test_limit(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    for name in ("a", "b", "c"):
        _write_row(queue_dir / f"{name}.yaml", **_base_row())
    manifest = _select(queue=[queue_dir], repo_root=tmp_path, limit=2)
    assert len(manifest.entries) == 2


def test_zero_spawn(tmp_path, monkeypatch):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    for name in ("a", "b", "c"):
        _write_row(queue_dir / f"{name}.yaml", **_base_row())

    calls = []
    original_init = subprocess.Popen.__init__

    def _counting_init(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _counting_init)
    _select(queue=[queue_dir], repo_root=tmp_path)
    assert calls == []


def test_open_row_set_matches_load_family_records(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "open-1.yaml", **_base_row(status="open"))
    _write_row(queue_dir / "open-2.yaml", **_base_row(status="open"))
    _write_row(queue_dir / "closed-1.yaml", **_base_row(status="closed"))

    manifest = _select(
        queue=[queue_dir], repo_root=tmp_path, where=[[["status", "==", "open"]]]
    )
    selector_ids = {e.row_id for e in manifest.entries}

    family_records = queue_family.load_family_records("bug-backlog", tmp_path, where="status = open")
    family_ids = {Path(r["path"]).stem for r in family_records}

    assert selector_ids == family_ids == {"open-1", "open-2"}


def _append_ledger(repo_root: Path, profile: str, row_id: str, **fields) -> None:
    ledger_dir = repo_root / "state" / "queue-grind" / profile
    ledger_dir.mkdir(parents=True, exist_ok=True)
    record = dict(
        profile=profile,
        row_id=row_id,
        digest="",
        stage="triage",
        verdict="REAL",
        outcome="done",
        evidence_file="",
        run_stamp="2026-09-21T00:00:00Z",
    )
    record.update(fields)
    with (ledger_dir / f"{row_id}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def test_ledger_fold_in_skip_on_digest_match(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    row_path = queue_dir / "a.yaml"
    _write_row(row_path, **_base_row())
    digest = __import__("hashlib").sha256(row_path.read_bytes()).hexdigest()
    _append_ledger(tmp_path, "fixture", "a", digest=digest, stage="triage")

    manifest = _select(queue=[queue_dir], repo_root=tmp_path)
    assert manifest.entries[0].skip_stages == ("triage",)


def test_ledger_fold_in_rerun_on_digest_mismatch(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    row_path = queue_dir / "a.yaml"
    _write_row(row_path, **_base_row())
    _append_ledger(tmp_path, "fixture", "a", digest="stale-digest", stage="triage")

    manifest = _select(queue=[queue_dir], repo_root=tmp_path)
    assert manifest.entries[0].skip_stages == ()


def test_ledger_route_to_excluded_and_declined(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    row_path = queue_dir / "a.yaml"
    _write_row(row_path, **_base_row())
    digest = __import__("hashlib").sha256(row_path.read_bytes()).hexdigest()
    _append_ledger(tmp_path, "fixture", "a", digest=digest, outcome="route-to-learn-lessons")

    manifest = _select(queue=[queue_dir], repo_root=tmp_path)
    assert manifest.entries == ()
    assert len(manifest.declined) == 1
    assert manifest.declined[0].row_id == "a"
    assert "route-to-learn-lessons" in manifest.declined[0].reason


def test_ledger_route_to_re_emit_is_idempotent(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    row_path = queue_dir / "a.yaml"
    _write_row(row_path, **_base_row())
    digest = __import__("hashlib").sha256(row_path.read_bytes()).hexdigest()
    _append_ledger(tmp_path, "fixture", "a", digest=digest, outcome="route-to-learn-lessons")

    first = _select(queue=[queue_dir], repo_root=tmp_path)
    second = _select(queue=[queue_dir], repo_root=tmp_path)
    assert first.digest == second.digest
    assert first.declined == second.declined


def test_manifest_digest_is_deterministic(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row())
    m1 = _select(queue=[queue_dir], repo_root=tmp_path)
    m2 = _select(queue=[queue_dir], repo_root=tmp_path)
    assert m1.digest == m2.digest
    assert isinstance(m1, Manifest)
