
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import queue_family
from coordinator_core.ops.dispatch_emit.queue_select import (
    ClaimedEntry,
    DuplicateStemError,
    Manifest,
    MissingRowIdError,
    RouteToRefusedError,
    WhereTermError,
    live_row_ids,
    select_rows,
)
from coordinator_core.session import claims, liveness


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
    if kwargs.get("source") is not None:
        kwargs.setdefault("source_row_dir", Path(kwargs["repo_root"]) / "run" / "source-rows")
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


def test_source_replaces_queue_read_and_materialises_closable_rows(tmp_path, monkeypatch):
    from coordinator_core.contract import grind_vocab as _grind_vocab
    from coordinator_core.ops.dispatch_emit import queue_select as _qs

    monkeypatch.setattr(_grind_vocab, "SOURCE_OPS", frozenset({"fake.source"}))
    queue_dir = tmp_path / "state" / "lessons"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row())
    _write_row(queue_dir / "b.yaml", **_base_row())
    monkeypatch.setattr(
        _qs, "_call_source_op", lambda op, args, root: {"records": [{"title": "x"}, {"title": "y"}]}
    )
    source = {"op": "fake.source", "args": {}}
    manifest = _select(queue=[queue_dir], repo_root=tmp_path, source=source)
    assert len(manifest.entries) == 2
    for entry in manifest.entries:
        row_file = tmp_path / entry.path
        assert row_file.is_file() and row_file.stem == entry.row_id
    assert _qs.live_row_ids([queue_dir], row_id_key="@stem", repo_root=tmp_path, source=source) == {
        e.row_id for e in manifest.entries
    }


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


def test_unselected_names_rows_the_where_and_limit_leave_out(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(severity="P0"))
    _write_row(queue_dir / "b.yaml", **_base_row(severity="P1"))
    _write_row(queue_dir / "c.yaml", **_base_row(severity="P3"))
    where = [[["severity", "in", ["P0", "P1"]]]]
    manifest = _select(queue=[queue_dir], repo_root=tmp_path, where=where)
    assert manifest.unselected == ("c",)
    limited = _select(queue=[queue_dir], repo_root=tmp_path, where=where, limit=1)
    assert limited.unselected == ("b", "c")


def test_unselected_is_outside_the_manifest_digest(tmp_path):
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_row(queue_dir / "a.yaml", **_base_row(severity="P0"))
    _write_row(queue_dir / "b.yaml", **_base_row(severity="P3"))
    narrow = _select(queue=[queue_dir], repo_root=tmp_path, where=[[["severity", "==", "P0"]]])
    (queue_dir / "b.yaml").unlink()
    alone = _select(queue=[queue_dir], repo_root=tmp_path, where=[[["severity", "==", "P0"]]])
    assert narrow.digest == alone.digest


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


@pytest.fixture
def claim_env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    live: set = set()
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in live)
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)

    class Env:
        root = tmp_path
        queue = queue_dir
        live_set = live

        @staticmethod
        def add_rows(*stems):
            for stem in stems:
                _write_row(queue_dir / f"{stem}.yaml", **_base_row())

        @staticmethod
        def claim_as(sid, stem, alive=True):
            monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
            if alive:
                live.add(sid)
            assert claims.claim_artifact(
                "row",
                f"state/bug-backlog/{stem}.yaml",
                baton_repo_root=str(tmp_path),
                cwd=str(tmp_path),
            )

        @staticmethod
        def select(**kw):
            return _select(queue=[queue_dir], repo_root=tmp_path, **kw)

    return Env


def test_row_claimed_live_by_another_sid_is_skipped_and_recorded(claim_env):
    claim_env.add_rows("a", "b")
    claim_env.claim_as("sid-a", "a")
    m = claim_env.select(claimant_sid="sid-b")
    assert [e.row_id for e in m.entries] == ["b"]
    assert m.claimed == (ClaimedEntry(row_id="a", holder_session="sid-a"),)
    assert "a" not in m.unselected


def test_limit_applies_after_the_claim_drop(claim_env):
    claim_env.add_rows("a", "b", "c")
    claim_env.claim_as("sid-a", "a")
    m = claim_env.select(claimant_sid="sid-b", limit=1)
    assert [e.row_id for e in m.entries] == ["b"]


def test_row_claimed_by_claimant_itself_is_selected(claim_env):
    claim_env.add_rows("a")
    claim_env.claim_as("sid-a", "a")
    m = claim_env.select(claimant_sid="sid-a")
    assert [e.row_id for e in m.entries] == ["a"]
    assert m.claimed == ()


def test_dead_holder_and_expired_lease_do_not_block(claim_env, monkeypatch):
    claim_env.add_rows("a", "b")
    claim_env.claim_as("sid-a", "a")
    claim_env.claim_as("sid-c", "b")
    claim_env.live_set.discard("sid-a")
    monkeypatch.setattr(claims, "brief_lease_expired", lambda d: Path(d).name.endswith("--b"))
    m = claim_env.select(claimant_sid="sid-b")
    assert [e.row_id for e in m.entries] == ["a", "b"]
    assert m.claimed == ()


def test_claimant_none_blocks_on_any_live_claim(claim_env):
    claim_env.add_rows("a", "b")
    claim_env.claim_as("sid-a", "a")
    m = claim_env.select(claimant_sid=None)
    assert [e.row_id for e in m.entries] == ["b"]
    assert [c.row_id for c in m.claimed] == ["a"]


def test_claimed_row_stays_live(claim_env):
    claim_env.add_rows("a", "b")
    claim_env.claim_as("sid-a", "a")
    ids = live_row_ids([claim_env.queue], row_id_key="@stem", repo_root=claim_env.root)
    assert ids == {"a", "b"}


def test_select_over_20_rows_with_5_claimed_spawns_nothing(claim_env, monkeypatch):
    # Liveness is stubbed here; the real probe's spawn cost is bounded in test_row_claims_perf.py.
    stems = [f"r{i:02d}" for i in range(20)]
    claim_env.add_rows(*stems)
    for stem in stems[:5]:
        claim_env.claim_as("sid-a", stem)
    with _count_spawns_attributed(monkeypatch) as spawns:
        m = claim_env.select(claimant_sid="sid-b")
    assert spawns == []
    assert len(m.entries) == 15 and len(m.claimed) == 5


def test_claimed_is_not_in_the_digest(claim_env):
    claim_env.add_rows("a", "b")
    claim_env.claim_as("sid-a", "a")
    with_claim = claim_env.select(claimant_sid="sid-b")
    (claim_env.queue / "a.yaml").unlink()
    without_row = claim_env.select(claimant_sid="sid-b")
    assert with_claim.claimed and not without_row.claimed
    assert with_claim.digest == without_row.digest
