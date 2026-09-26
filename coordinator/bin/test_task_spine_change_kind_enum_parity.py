"""
test_task_spine_change_kind_enum_parity.py — enum-parity CHARACTERIZATION test.

Spec backlink: pln-full-coverage-planning-posture-bca96f § C1 (pinned task-spine
contract) — the same backlink plan-tasks.schema.json's own `change_kind`
description carries.

Re-authors leg 1 (C1 — enum-parity guard) of the retired shell aggregator
`test-plan-tasks-schema-enum-parity.sh`. That file was already absent from
disk when `coordinator/tests/test_run_plan_tasks_spine_suites.py` ported the
other three legs (see that module's docstring, final paragraph) and was
dropped rather than re-authored. This test closes the resulting coverage gap:
nothing else in the tree asserts task-spine `change_kind` enum parity — the
three enum-parity guards that do exist
(`test_pickup_kind_enum_parity.py`, `test_handoff_category_enum_parity.py`,
`test_queue_append_central_root_parity.py`) cover unrelated enums.

SCOPE: three assertions, each parsed directly from its own on-disk source
(no hand-maintained copy of any enum lives in this test):

  1. `coordinator_core/frontmatter/schemas/plan-tasks.schema.json`'s
     `change_kind.enum` (the WIDER UNIVERSAL change-kind enum the spine
     authors against) must set-equal the union of the two intra-repo slices
     that jointly define it: `coordinator-harvest-deferrals.py`'s
     `_QUEUE_ELIGIBLE_CHANGE_KINDS` (routes to the improvement queue) and its
     `_LESSON_PROMOTE_CHANGE_KINDS` (routes to the lessons outbox instead).
  2. `_QUEUE_ELIGIBLE_CHANGE_KINDS` must be a subset of
     `coordinator_core/frontmatter/schemas/improvement-queue.schema.json`'s
     own `change_kind.enum` — the harvest-eligible slice can never claim a
     value the queue schema itself would reject on write.
  3. Both vendored schemas' `change_kind` property descriptions must still
     carry the literal SSOT citation
     (`docs/wiki/lessons-outbox-schema.md § Change-kind enum`) rather than
     silently re-hosting the enum as if this repo owned it.

The `change_kind` enum's true SSOT is DoE-claude's
`docs/wiki/lessons-outbox-schema.md`, resolved only via
`coordinator_registry.doe_root()` elsewhere in this suite (see
`test_pickup_kind_enum_parity.py`'s third leg) when a value must be read FROM
it. This test never needs to: both slices it compares are already vendored
into this repo's own schemas, so no DoE-clone dependency is introduced here.

Run with: python3 -m pytest coordinator/bin/test_task_spine_change_kind_enum_parity.py
"""

from __future__ import annotations

import json
import os
import re

_SSOT_CITATION = "docs/wiki/lessons-outbox-schema.md § Change-kind enum"


def _repo_bin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(_repo_bin_dir()))


def _harvest_deferrals_path() -> str:
    return os.path.join(_repo_bin_dir(), "coordinator-harvest-deferrals.py")


def _plan_tasks_schema_path() -> str:
    return os.path.join(
        _repo_root(), "coordinator_core", "frontmatter", "schemas", "plan-tasks.schema.json"
    )


def _improvement_queue_schema_path() -> str:
    return os.path.join(
        _repo_root(),
        "coordinator_core",
        "frontmatter",
        "schemas",
        "improvement-queue.schema.json",
    )


def _parse_frozenset_literal(content: str, name: str, path: str) -> set[str]:
    m = re.search(rf'{re.escape(name)}\s*=\s*frozenset\(\s*\{{(.*?)\}}\s*\)', content, re.DOTALL)
    if not m:
        raise AssertionError(
            f"could not locate '{name} = frozenset({{...}})' in {path} — "
            "declaration shape has changed; update this test's parser."
        )
    items = re.findall(r'"([^"]+)"', m.group(1))
    if not items:
        raise AssertionError(
            f"{name} in {path} parsed empty — regex/shape mismatch."
        )
    return set(items)


def _queue_eligible_and_lesson_promote_kinds() -> tuple[set[str], set[str]]:
    path = _harvest_deferrals_path()
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    queue_eligible = _parse_frozenset_literal(content, "_QUEUE_ELIGIBLE_CHANGE_KINDS", path)
    lesson_promote = _parse_frozenset_literal(content, "_LESSON_PROMOTE_CHANGE_KINDS", path)
    return queue_eligible, lesson_promote


def _change_kind_property(schema_path: str) -> dict:
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    try:
        return schema["properties"]["change_kind"]
    except KeyError as exc:
        raise AssertionError(
            f"could not locate properties.change_kind in {schema_path} — "
            "schema shape has changed; update this test's parser."
        ) from exc


def _change_kind_enum(schema_path: str) -> set[str]:
    items = _change_kind_property(schema_path).get("enum")
    if not items:
        raise AssertionError(
            f"properties.change_kind.enum in {schema_path} parsed empty — schema/shape mismatch."
        )
    return set(items)


def test_spine_change_kind_enum_matches_universal_union() -> None:
    schema_path = _plan_tasks_schema_path()
    spine_kinds = _change_kind_enum(schema_path)
    queue_eligible, lesson_promote = _queue_eligible_and_lesson_promote_kinds()
    universal = queue_eligible | lesson_promote

    if spine_kinds == universal:
        return

    detail_parts = []
    missing_from_universal = spine_kinds - universal
    extra_in_universal = universal - spine_kinds
    if missing_from_universal:
        detail_parts.append(
            f"{schema_path} change_kind.enum admits but neither harvest slice routes: "
            f"{sorted(missing_from_universal)}"
        )
    if extra_in_universal:
        detail_parts.append(
            f"a harvest slice routes but {schema_path} change_kind.enum does not admit: "
            f"{sorted(extra_in_universal)}"
        )
    raise AssertionError(
        "plan-tasks change_kind enum == "
        "_QUEUE_ELIGIBLE_CHANGE_KINDS | _LESSON_PROMOTE_CHANGE_KINDS: "
        + ("; ".join(detail_parts))
    )


def test_harvest_eligible_slice_is_subset_of_improvement_queue_enum() -> None:
    """`_QUEUE_ELIGIBLE_CHANGE_KINDS` must be a subset of
    improvement-queue.schema.json's own `change_kind.enum`.

    The harvest slice decides which spine rows get routed to
    `coordinator-queue-append --schema improvement-queue`; a slice member the
    queue schema itself does not admit would route a row into a write the
    schema rejects at validation time, deferred all the way to harvest.
    """
    queue_eligible, _lesson_promote = _queue_eligible_and_lesson_promote_kinds()
    schema_path = _improvement_queue_schema_path()
    queue_schema_kinds = _change_kind_enum(schema_path)

    extra = queue_eligible - queue_schema_kinds
    assert not extra, (
        f"_QUEUE_ELIGIBLE_CHANGE_KINDS routes {sorted(extra)} to "
        f"coordinator-queue-append, but {schema_path} change_kind.enum does not "
        "admit them"
    )


def test_change_kind_schemas_cite_ssot() -> None:
    for schema_path in (_plan_tasks_schema_path(), _improvement_queue_schema_path()):
        description = _change_kind_property(schema_path).get("description", "")
        assert _SSOT_CITATION in description, (
            f"{schema_path} change_kind.description no longer cites the SSOT "
            f"({_SSOT_CITATION!r}) — enum values must point at the wiki table, "
            "not duplicate it uncited."
        )
