"""Tests for "ceremony.wiki_candidates": registry, window, selection, ranking, caps, spawn shape.

Every case drives the registered handler against a real throwaway git repo under tmp_path, with
ledger rows appended through `plan_completion_ledger` (the production writer's row shape).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.wiki_candidates as wc
from coordinator_core.ipc import _REGISTRY

_OP = "ceremony.wiki_candidates"
_WIKI = "docs" + "/wiki/"
_NOW = datetime.now(timezone.utc)


def test_op_registered() -> None:
    assert _OP in _REGISTRY


def test_completed_statuses_are_members_of_the_plan_schema_status_enum() -> None:
    schema_path = Path(wc.__file__).parents[2] / "frontmatter" / "schemas" / "plan.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert wc.COMPLETED_STATUSES <= set(schema["properties"]["status"]["enum"])
    assert "complete" not in wc.COMPLETED_STATUSES
