"""program_facts: mapping determinism, id safety, schema validity and the write through the op."""
import copy
import json
import re
from pathlib import Path

import pytest

from coordinator_core.ops.rea_ledger_write import _rea_ledger_write, _safe_component, _schema_errors
from coordinator_core.rea_ingest import program_facts
from coordinator_core.rea_ledger.canonical import canonical_bytes

SAMPLE = json.loads((Path(__file__).resolve().parent / "fixtures" / "sample-export.json").read_text(encoding="utf-8"))
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _files(repo):
    return sorted(p.relative_to(repo).as_posix() for p in (repo / "state").rglob("*") if p.is_file())


def test_mapping_has_one_claim_and_all_four_kinds():
    recs = program_facts.to_records(SAMPLE)
    assert len(recs["claim"]) == 1
    kinds = {r["id"].split("-")[0] for r in recs["evidence_record"]}
    assert kinds == {"function", "xref", "import", "string"}
    assert len(recs["evidence_record"]) == 4 + 3 + 1 + 1
    assert all(r["claim_id"] == recs["claim"][0]["id"] for r in recs["evidence_record"])


def test_mapping_is_deterministic_and_never_reads_the_clock():
    a = program_facts.to_records(SAMPLE)
    b = program_facts.to_records(copy.deepcopy(SAMPLE))
    assert canonical_bytes(a) == canonical_bytes(b)
    assert {r["observed_at"] for r in a["evidence_record"]} == {program_facts.OBSERVED_AT_UNSET}
    given = program_facts.to_records(SAMPLE, "2026-11-02T00:00:00Z")
    assert {r["observed_at"] for r in given["evidence_record"]} == {"2026-11-02T00:00:00Z"}


def test_records_validate_and_ids_are_safe_and_unique():
    recs = program_facts.to_records(SAMPLE)
    ids = [r["id"] for r in recs["evidence_record"]]
    assert len(ids) == len(set(ids))
    for r in recs["evidence_record"]:
        assert ID_RE.match(r["id"]) and len(r["id"]) <= 128 and _safe_component(r["id"])
        assert _schema_errors("evidence_record", r) == []
        assert r["stance"] == "context" and r["source_kind"] == "command-output"
        assert r["source_ref"] == f"ghidra:11.1.2:sha256:{SAMPLE['program']['sha256']}"
    assert _schema_errors("claim", recs["claim"][0]) == []


def test_excerpt_is_the_canonical_fact_and_locators_name_the_fact():
    recs = {}
    for r in program_facts.to_records(SAMPLE)["evidence_record"]:
        recs.setdefault(r["id"].split("-")[0], r)
    assert json.loads(recs["import"]["excerpt"]) == SAMPLE["imports"][0]
    assert recs["import"]["locator"] == "msvcrt.dll!puts"
    assert recs["string"]["locator"] == "0x140002000"
    assert recs["function"]["locator"] == "0x140001000"


def test_hostile_and_repeated_ids_stay_safe():
    doc = copy.deepcopy(SAMPLE)
    doc["functions"][0]["entry"] = "../../x/" + "a" * 200
    doc["strings"].append(dict(doc["strings"][0], value="again"))
    recs = program_facts.to_records(doc)["evidence_record"]
    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids))
    for i in ids:
        assert ID_RE.match(i) and len(i) <= 128 and _safe_component(i)


def test_record_writes_claim_then_evidence_through_the_op(repo):
    out = program_facts.record(SAMPLE, repo)
    assert out == {"claim": 1, "evidence_record": 9}
    files = _files(repo)
    assert sum("/claim/" in f for f in files) == 1
    assert sum("/evidence_record/" in f for f in files) == 9
    again = program_facts.record(SAMPLE, repo)
    assert again == {"claim": 0, "evidence_record": 0}
    assert _files(repo) == files


def test_record_goes_through_the_registered_op(repo, monkeypatch):
    from coordinator_core import ipc

    calls = []

    def spy(params, repo_root=None):
        calls.append(params["kind"])
        return _rea_ledger_write(params, repo_root)

    monkeypatch.setattr(ipc, "get_op_handler", lambda name, msg=None: spy if name == "rea_ledger.write" else None)
    program_facts.record(SAMPLE, repo)
    assert calls == ["claim", "evidence_record"]


def test_op_refusal_raises_and_unregistered_op_raises(repo, monkeypatch):
    from coordinator_core import ipc

    monkeypatch.setattr(ipc, "get_op_handler", lambda name, msg=None: lambda p, r=None: {"error": "refused"})
    with pytest.raises(program_facts.ProgramFactsWriteError, match="refused"):
        program_facts.record(SAMPLE, repo)
    monkeypatch.setattr(ipc, "get_op_handler", lambda name, msg=None: None)
    with pytest.raises(program_facts.ProgramFactsWriteError, match="not registered"):
        program_facts.record(SAMPLE, repo)


def test_export_missing_a_contract_field_is_refused_before_any_write(repo):
    doc = copy.deepcopy(SAMPLE)
    del doc["imports"]
    with pytest.raises(KeyError):
        program_facts.record(doc, repo)
    assert not (repo / "state").exists()
