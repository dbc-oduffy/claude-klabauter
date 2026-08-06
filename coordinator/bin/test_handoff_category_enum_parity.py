"""
test_handoff_category_enum_parity.py — enum-parity CHARACTERIZATION test.

Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md

SCOPE: the handoff-schema-family category enum is declared in two places that
must stay in lockstep, and this test parses both directly from disk and
asserts set-equality — no leg is covered transitively via a hand-maintained
comment:

  1. coordinator_core/frontmatter/schemas/handoff.schema.json — `category`
     property's `enum` array (schema authority)
  2. coordinator/bin/coordinator-doc-new                       — `_HANDOFF_CATEGORY_ENUM`
     (scaffolder-side copy, consumed by `_validate_category` and every
     handoff-schema-family scaffolder's default)

Neither is imported as code — coordinator-doc-new is a standalone CLI script,
not a package, and the schema is JSON consumed by a JS validator elsewhere —
so both are parsed via targeted regex/JSON-load against their well-known
literal shapes. A drift between the two (coordinator-doc-new accepting a
category the schema rejects, or vice versa) fails this test loud, closing the
exact drift class the source memo's incident occurred under: a caller-supplied
category the schema itself does not admit slips through unvalidated at
scaffold time and only surfaces at gate-recheck/claim-handoff stamp time, in
a different session.

Run with: python3 -m pytest coordinator/bin/test_handoff_category_enum_parity.py
"""

from __future__ import annotations

import json
import os
import re
import sys


def _repo_bin_dir() -> str:
    """Absolute path to the coordinator/bin directory this test lives in."""
    return os.path.dirname(os.path.abspath(__file__))


def _coordinator_doc_new_path() -> str:
    return os.path.join(_repo_bin_dir(), "coordinator-doc-new")


def _handoff_schema_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(_repo_bin_dir())),
        "coordinator_core",
        "frontmatter",
        "schemas",
        "handoff.schema.json",
    )


def _parse_category_enum_from_cli(path: str) -> set[str]:
    """Extract the `_HANDOFF_CATEGORY_ENUM = (...)` tuple literal from coordinator-doc-new.

    Matches the exact declaration shape (one quoted item per line, or inline):
        _HANDOFF_CATEGORY_ENUM = (
            "roadmap",
            "infra",
            ...
        )
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'_HANDOFF_CATEGORY_ENUM\s*=\s*\(([^)]*)\)', content)
    if not m:
        raise AssertionError(
            f"could not locate '_HANDOFF_CATEGORY_ENUM = (...)' in {path} — "
            "CLI enum declaration shape has changed; update this test's parser."
        )
    items = re.findall(r'"([^"]+)"', m.group(1))
    if not items:
        raise AssertionError(
            f"_HANDOFF_CATEGORY_ENUM tuple in {path} parsed empty — regex/shape mismatch."
        )
    return set(items)


def _parse_category_enum_from_schema(path: str) -> set[str]:
    """Extract the `category.enum` array from handoff.schema.json via a real JSON load
    (the schema is well-formed JSON, unlike the two polyglot/prose sources this test's
    sibling test_pickup_kind_enum_parity.py must regex-parse)."""
    with open(path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    try:
        items = schema["properties"]["category"]["enum"]
    except KeyError as exc:
        raise AssertionError(
            f"could not locate properties.category.enum in {path} — "
            "schema shape has changed; update this test's parser."
        ) from exc
    if not items:
        raise AssertionError(
            f"properties.category.enum in {path} parsed empty — schema/shape mismatch."
        )
    return set(items)


def test_cli_category_enum_matches_schema() -> None:
    """coordinator-doc-new's _HANDOFF_CATEGORY_ENUM must set-equal the handoff
    schema's category enum.

    Fails loud (with the actual symmetric difference) on divergence in either
    direction — the CLI accepting a value the schema rejects, or the schema
    admitting a value the CLI's _validate_category does not recognize.
    """
    name = "coordinator-doc-new _HANDOFF_CATEGORY_ENUM == handoff.schema.json category.enum"
    cli_categories = _parse_category_enum_from_cli(_coordinator_doc_new_path())
    schema_categories = _parse_category_enum_from_schema(_handoff_schema_path())

    if cli_categories == schema_categories:
        return

    detail_parts = []
    missing_from_cli = schema_categories - cli_categories
    extra_in_cli = cli_categories - schema_categories
    if missing_from_cli:
        detail_parts.append(
            f"schema admits but coordinator-doc-new does not recognize: {sorted(missing_from_cli)}"
        )
    if extra_in_cli:
        detail_parts.append(
            f"coordinator-doc-new recognizes but schema does not admit: {sorted(extra_in_cli)}"
        )
    raise AssertionError(f"{name}: " + ("; ".join(detail_parts)))

