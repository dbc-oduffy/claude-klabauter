"""MEMO_RULE_CONTRACT covers every memo cross-field rule; the digest names the whole contract."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.frontmatter import schema_validate
from coordinator_core.frontmatter.memo_contract import MEMO_RULE_CONTRACT, memo_contract_digest
from coordinator_core.ops.fleet.memo_kinds import VALID_KINDS

_CONTRACT_DIR = Path(schema_validate.__file__).resolve().parent.parent / "contract"


def _schema(name: str) -> dict:
    return json.loads((_CONTRACT_DIR / name).read_text(encoding="utf-8"))


def test_contract_covers_every_non_grandfather_rule():
    rules = {r.__name__ for r in schema_validate._MEMO_CROSS_FIELD_RULES}
    rules.discard("_memo_cf_grandfather")
    assert set(MEMO_RULE_CONTRACT) == rules


def test_digest_names_the_whole_cross_repo_memo_contract():
    schema = _schema("cross-repo-memo.schema.json")
    digest = memo_contract_digest(schema)
    for field in schema["required"]:
        assert field in digest
    for value in schema["properties"]["status"]["enum"]:
        assert value in digest
    for value in schema["properties"]["delivery_mode"]["enum"]:
        assert value in digest
    for kind in VALID_KINDS:
        assert kind in digest
    assert "picked_up_by" in digest
    assert "\n" not in digest


def test_digest_for_archived_memo_uses_its_own_required_list():
    digest = memo_contract_digest(_schema("archived-memo.schema.json"))
    for field in ("from", "to", "status"):
        assert field in digest
    assert "delivery_mode" not in digest.split("; kind")[0]


def test_digest_states_no_numeric_limit():
    assert "summary is one line within the length cap" in memo_contract_digest(
        _schema("cross-repo-memo.schema.json")
    )
