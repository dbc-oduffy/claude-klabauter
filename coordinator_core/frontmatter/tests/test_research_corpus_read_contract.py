"""Pin test for docs/reference/research-corpus-read-contract.md's field mapping.

Zero-spawn, pure pathlib + json/yaml. Enforces the tripwire the contract
doc names: a `status: live` row must name a property that already exists
on the vendored schema it cites, and a `status: proposed` row must name a
property that does NOT exist there yet — so the doc cannot drift from the
schema in either direction. When DoE-claude re-vendors research-synthesis
at 1.1.0 (C7), the proposed-absent assertion goes red until the doc flips
those rows to live, in the same commit as the re-vendor.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DOC = _REPO_ROOT / "docs" / "reference" / "research-corpus-read-contract.md"
_SCHEMAS_DIR = _REPO_ROOT / "coordinator_core" / "frontmatter" / "schemas"

_SCHEMA_FILES = {
    "research-synthesis": _SCHEMAS_DIR / "research-synthesis.schema.json",
    "research-claim": _SCHEMAS_DIR / "research-claim.schema.json",
}

_FENCE_RE = re.compile(
    r"```yaml corpus-contract-map\n(?P<body>.*?)```", re.DOTALL
)


def _load_mapping_rows() -> list[dict]:
    text = _CONTRACT_DOC.read_text(encoding="utf-8")
    match = _FENCE_RE.search(text)
    assert match is not None, (
        "docs/reference/research-corpus-read-contract.md must contain exactly one "
        "```yaml corpus-contract-map fenced block"
    )
    rows = yaml.safe_load(match.group("body"))
    assert isinstance(rows, list) and rows, "corpus-contract-map block must be a non-empty list"
    return rows


def _schema_properties(schema_name: str) -> set[str]:
    schema_path = _SCHEMA_FILES[schema_name]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return set(schema.get("properties", {}).keys())


def _fields_in(source_field: str) -> list[str]:
    # source_field is sometimes a single property, sometimes a slash-separated
    # list like "market_scope / target_uid / target_label" or
    # "question / topic_facets".
    return [part.strip() for part in source_field.split("/") if part.strip()]


def test_corpus_contract_map_block_parses():
    rows = _load_mapping_rows()
    for row in rows:
        assert "contract_field" in row
        assert "schema" in row
        assert "source_field" in row
        assert "status" in row


def test_live_rows_name_properties_present_in_the_vendored_schema():
    rows = _load_mapping_rows()
    for row in rows:
        if row["status"] != "live":
            continue
        schema_name = row["schema"]
        if schema_name not in _SCHEMA_FILES:
            # "derived" rows (dive_id, CorpusResult.coverage_scope/dark) have no
            # vendored schema to check against.
            continue
        properties = _schema_properties(schema_name)
        for field in _fields_in(row["source_field"]):
            assert field in properties, (
                f"live row {row['contract_field']!r} names {field!r} on "
                f"{schema_name}, which is not a property of the vendored schema"
            )


def test_proposed_rows_name_properties_absent_from_the_vendored_schema():
    rows = _load_mapping_rows()
    for row in rows:
        if row["status"] != "proposed":
            continue
        schema_name = row["schema"]
        assert schema_name in _SCHEMA_FILES, (
            f"proposed row {row['contract_field']!r} names schema {schema_name!r}, "
            "which has no vendored counterpart to be absent from"
        )
        properties = _schema_properties(schema_name)
        for field in _fields_in(row["source_field"]):
            assert field not in properties, (
                f"proposed row {row['contract_field']!r} names {field!r} on "
                f"{schema_name}, which is ALREADY present in the vendored schema — "
                "the doc row must flip to status: live in the same commit that "
                "re-vendors the schema"
            )


def test_research_claim_schema_still_declares_example_market_data_repo_consumer():
    schema = json.loads(_SCHEMA_FILES["research-claim"].read_text(encoding="utf-8"))
    assert "example-market-data-repo" in schema.get("x-external-consumers", [])
