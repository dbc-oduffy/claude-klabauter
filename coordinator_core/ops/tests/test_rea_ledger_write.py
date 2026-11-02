"""rea_ledger.write: registration, canonical-import refusal, chain checks, path safety, append-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops.rea_ledger_write import _rea_ledger_write as write
from coordinator_core.rea_ledger.canonical import canonical_bytes, revision_digest

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "contract" / "vendored" / "unknown-revision" / "v1" / "vectors.json")
    .read_text(encoding="utf-8")
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _text(rec: dict) -> str:
    return canonical_bytes(rec).decode("utf-8")


def _unknown(rev: int, prev, uid: str = "u-1", **over) -> dict:
    rec = {
        "unknown_id": uid, "revision": rev, "previous_revision_digest": prev, "question": "q?",
        "coverage": "partial", "coverage_scope": "claim", "epistemic_mode": "observed",
        "source_authority": "shipped-artifact", "observed_at": "2026-11-01T12:00:00+00:00",
    }
    rec.update(over)
    return rec


def _evidence(ev_id: str = "ev-1", **over) -> dict:
    rec = {
        "schema_version": "evidence-record/v1", "id": ev_id, "claim_id": "c1", "stance": "supports",
        "source_kind": "file", "source_ref": "a.c", "locator": None, "excerpt": None,
        "observed_at": "2026-11-02T00:00:00Z", "observer": "s1",
    }
    rec.update(over)
    return rec


def _ledger(repo) -> Path:
    return repo / "state" / "rea-ledger"


def _snapshot(repo) -> dict:
    root = _ledger(repo)
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()} if root.exists() else {}


def test_registration_surfaces():
    assert ipc._REGISTRY.get("rea_ledger.write") is not None
    assert _registry_map.OP_MODULE_MAP["rea_ledger.write"] == "coordinator_core.ops.rea_ledger_write"
    assert OP_KEY_SCOPE["rea_ledger.write"] == "common_dir"
    assert OP_CLASSIFICATION["rea_ledger.write"] is OpClass.MUTATING


def test_claim_lands_content_addressed_and_same_id_claims_both_land(repo):
    a = {"id": "c1", "claim_text": "alpha"}
    b = {"id": "c1", "claim_text": "beta"}
    assert write({"kind": "claim", "records": [_text(a), _text(b)]}, repo_root=repo) == {"written": 2, "kind": "claim"}
    files = sorted((_ledger(repo) / "claim").iterdir())
    assert len(files) == 2 and all(f.name.endswith(".json") and f.stem != "c1" for f in files)
    assert {f.read_bytes() for f in files} == {canonical_bytes(a), canonical_bytes(b)}
    assert write({"kind": "claim", "records": [_text(a)]}, repo_root=repo) == {"written": 0, "kind": "claim"}


def test_linked_worktree_style_root_writes_one_ledger(repo):
    assert write({"kind": "claim", "records": [_text({"id": "c", "claim_text": "t"})]}, repo_root=repo / ".git")["written"] == 1
    assert (_ledger(repo) / "claim").is_dir()


def test_non_canonical_bytes_refused_and_batch_writes_nothing(repo):
    good = _text(_evidence("ev-ok"))
    pretty = json.dumps(_evidence("ev-2"), indent=2)
    res = write({"kind": "evidence_record", "records": [good, pretty]}, repo_root=repo)
    assert "record 1" in res["error"] and "canonical" in res["error"]
    assert _snapshot(repo) == {}


def test_vector_refusal_import_is_refused(repo):
    v = _VECTORS["refusal"][0]
    res = write({"kind": "unknown_revision", "records": [v["import_text"]]}, repo_root=repo)
    assert "error" in res and _snapshot(repo) == {}


def test_digest_parity_with_vendored_vector(repo):
    first = _VECTORS["valid"][0]
    assert write({"kind": "unknown_revision", "records": [_text(first["record"])]}, repo_root=repo)["written"] == 1
    assert revision_digest(first["record"]) == first["revision_digest"]
    second = _VECTORS["valid"][1]["record"]
    assert write({"kind": "unknown_revision", "records": [_text(second)]}, repo_root=repo)["written"] == 1


def test_chain_accepts_then_refuses_wrong_previous_digest(repo):
    r1 = _unknown(1, None)
    assert write({"kind": "unknown_revision", "records": [_text(r1)]}, repo_root=repo)["written"] == 1
    path = _ledger(repo) / "unknown_revision" / "u-1.jsonl"
    before = path.read_bytes()
    bad = _unknown(2, "sha256:" + "0" * 64)
    res = write({"kind": "unknown_revision", "records": [_text(bad)]}, repo_root=repo)
    assert "previous_revision_digest differs" in res["error"] and path.read_bytes() == before
    good = _unknown(2, revision_digest(r1), question="next")
    assert write({"kind": "unknown_revision", "records": [_text(good)]}, repo_root=repo)["written"] == 1
    assert path.read_bytes().startswith(before) and path.read_bytes().count(b"\n") == 2


def test_chain_within_one_batch(repo):
    r1 = _unknown(1, None)
    r2 = _unknown(2, revision_digest(r1))
    r3 = _unknown(3, revision_digest(r2))
    assert write({"kind": "unknown_revision", "records": [_text(r1), _text(r2), _text(r3)]}, repo_root=repo)["written"] == 3


def test_second_revision_one_and_gap_are_refused(repo):
    r1 = _unknown(1, None)
    write({"kind": "unknown_revision", "records": [_text(r1)]}, repo_root=repo)
    assert "error" in write({"kind": "unknown_revision", "records": [_text(_unknown(1, None, question="x"))]}, repo_root=repo)
    assert "error" in write({"kind": "unknown_revision", "records": [_text(_unknown(3, revision_digest(r1)))]}, repo_root=repo)
    assert "error" in write({"kind": "unknown_revision", "records": [_text(_unknown(1, "sha256:" + "1" * 64, uid="u-2"))]}, repo_root=repo)


def test_unknown_missing_a_model_field_is_refused(repo):
    rec = _unknown(1, None)
    del rec["previous_revision_digest"]
    res = write({"kind": "unknown_revision", "records": [_text(rec)]}, repo_root=repo)
    assert "error" in res and _snapshot(repo) == {}


@pytest.mark.parametrize("bad_id", ["../x", "a/b", "a\\b", "..", ".", "C:evil", "a\x00b"])
def test_unsafe_ids_never_build_a_path(repo, bad_id):
    assert "error" in write({"kind": "unknown_revision", "records": [_text(_unknown(1, None, uid=bad_id))]}, repo_root=repo)
    assert "error" in write({"kind": "evidence_record", "records": [_text(_evidence(bad_id))]}, repo_root=repo)
    assert _snapshot(repo) == {} and not (repo.parent / "x").exists()


def test_evidence_create_once_and_schema_refusal(repo):
    ev = _evidence()
    assert write({"kind": "evidence_record", "records": [_text(ev)]}, repo_root=repo)["written"] == 1
    assert write({"kind": "evidence_record", "records": [_text(ev)]}, repo_root=repo)["written"] == 0
    assert "different bytes" in write({"kind": "evidence_record", "records": [_text(_evidence(excerpt="z"))]}, repo_root=repo)["error"]
    assert "error" in write({"kind": "evidence_record", "records": [_text(_evidence("ev-9", extra=1))]}, repo_root=repo)
    assert (_ledger(repo) / "evidence_record" / "ev-1.json").read_bytes() == canonical_bytes(ev)


def test_bad_params_are_errors_not_raises(repo):
    assert "error" in write({"kind": "nope", "records": ["{}"]}, repo_root=repo)
    assert "error" in write({"kind": "claim", "records": []}, repo_root=repo)
    assert "error" in write({"kind": "claim", "records": ["not json"]}, repo_root=repo)
    assert "error" in write({"kind": "claim", "records": ['{"a":NaN}']}, repo_root=repo)
    assert "error" in write({"kind": "claim", "records": ["{}"]}, repo_root=None)


_RACER = (
    "import sys, json; from pathlib import Path; "
    "from coordinator_core.ops.rea_ledger_write import _rea_ledger_write as w; "
    "print(json.dumps(w({'kind': 'unknown_revision', 'records': [sys.argv[2]]}, repo_root=Path(sys.argv[1]))))"
)


def test_concurrent_appends_of_the_same_revision_land_exactly_once(repo):
    import subprocess
    import sys

    r1 = _unknown(1, None)
    assert write({"kind": "unknown_revision", "records": [_text(r1)]}, repo_root=repo)["written"] == 1
    prev = revision_digest(r1)
    root = Path(__file__).resolve().parents[3]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER, str(repo), _text(_unknown(2, prev, question=f"racer-{n}"))],
            cwd=root, stdout=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for n in range(2)
    ]
    results = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert sorted("error" in r for r in results) == [False, True], results
    path = _ledger(repo) / "unknown_revision" / "u-1.jsonl"
    assert path.read_bytes().count(b"\n") == 2
