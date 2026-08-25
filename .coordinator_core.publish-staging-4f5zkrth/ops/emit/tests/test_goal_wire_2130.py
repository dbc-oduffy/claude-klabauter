"""2.13.0 goal-wire coverage — new Goal fields + InitiativeSummary.goals[] join.

Covers plan § C5 items (a)-(g) for
docs/plans/2026-07-13-makima-emit-goal-wire-2130-projection.md:

  (a) new-field emit round-trips green against the vendored 2.13.0 "goal" schema.
  (b) an old goal record carrying none of the new OPTIONAL keys still validates, and the
      always-emitted parent_goal_id present-as-null form validates.
  (c) parent_goal_id is ALWAYS present (never omitted) on emit; a fixture record with the
      key OMITTED is asserted to FAIL validate_array (Zod .strict() reject on the required
      field).
  (d) weekly_perceptible / key_results_status are absent-when-absent: a JSONL record
      lacking them emits a goal record that omits the keys entirely.
  (e) key_results_status re-projection drops evidence_source and per-KR
      weekly_perceptible (defensive re-projection against a store carrying stray keys),
      keeping only {id, text, kind, status}.
  (f) InitiativeSummary.goals[] cross-join: REMOVED 2026-08-23 with the emission writer
      (envelope._stamp_initiative_goals was reachable only from envelope.build). The
      section still stages _goal_ids; nothing pops it, and no live consumer wanted it —
      initiatives_serve.py has always had its own collector. Coverage went with the code.
      omit-on-unresolvable, and the makima-live omit-on-every-initiative path; also that no
      staging _goal_ids key survives to the wire record.
  (g) per-repo keying (FOLDS C4, plan AC6): a non-meta consumer repo's own goals_current +
      initiatives key on its own owner-qualified repo + declared_by_machine anchor, with no
      meta-repo-only gating — DR-025 (per-repo emission cutover; each repo emits under its
      own identity, never gated to a hardcoded meta-repo slug). Convention mirrors
      test_per_repo_emission_integration.py's _make_ctx factory.

Uses the vendored pin via validate.validate_array (entity_name="goal"); tests that call it
are gated by the requires_vendor_pin session fixture (conftest.py) — in-process JSON Schema
validation (2026-07-21, node/Zod validator retired — see validate.py module docstring), no
node/zod operational probe required.

Coupled change: this chunk also added a single-key ``parent_goal_id: null`` line to the
frozen ``goals_current`` record in ``fixtures/golden-cockpit-emission.json``, whose
regression net is ``test_emit_parity.py``'s ``test_section_parity["goals"]`` — not this
file, which never opens the golden fixture directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import goals as goals_section
from coordinator_core.ops.emit.sections import initiatives as initiatives_section
from coordinator_core.ops.emit.sections.initiatives import _parse_goal_ids, _unquote
from coordinator_core.ops.emit.validate import ValidationError, validate_array

_ENTITY = "goal"


# ---------------------------------------------------------------------------
# Shared factory (mirrors test_per_repo_emission_integration._make_ctx)
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0000000000000000000000000000000000000000",
        git_sha_short="00000000",
        observed_at="2026-07-13T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _write_goal_log(ctx: EmitContext, machine: str, records: list[dict]) -> None:
    log_path = ctx.central_state_root / f"goals-log.{machine}.jsonl"
    log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _base_goal_record(**overrides) -> dict:
    record = {
        "goal_id": "goal-2130-base",
        "repo": "test-org/test-repo",
        "coordinator_root_path": ".",
        "period": "week",
        "period_value": "2026-W28",
        "declared_by_machine": "test-host",
        "declared_at": "2026-07-13T00:00:00Z",
        "text": "Base 2.13.0 fixture goal.",
        "status": "active",
    }
    record.update(overrides)
    return record


def _emitted_goal(ctx: EmitContext, machine: str, raw_record: dict) -> dict:
    """Write one JSONL record and return the single emitted goal dict from collect()."""
    _write_goal_log(ctx, machine, [raw_record])
    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 1, f"expected exactly 1 emitted record, got {len(records)}"
    return records[0]


# ---------------------------------------------------------------------------
# Direct parser unit tests for _parse_goal_ids/_unquote (DR-207 `goals:` array field).
#
# Review: code-reviewer (Finding 6, initiativesummary-goals-join) — the join-mechanics
# tests below (TestInitiativeGoalsJoin) synthesize `_goal_ids` directly as a Python list
# literal, bypassing the parser entirely. These tests exercise the parser itself against
# on-disk-shaped YAML text.
# ---------------------------------------------------------------------------

def _lines_and_idx(yaml_text: str, key_line_prefix: str = "goals:") -> tuple[list[str], int]:
    lines = yaml_text.splitlines()
    idx = next(i for i, l in enumerate(lines) if l.strip().startswith(key_line_prefix))
    return lines, idx


class TestParseGoalIds:
    def test_flow_list_two_ids(self) -> None:
        lines, idx = _lines_and_idx("id: x\nlabel: y\ngoals: [goal-a, goal-b]\n")
        val = lines[idx].split(":", 1)[1]
        assert _parse_goal_ids(val, lines, idx) == ["goal-a", "goal-b"]

    def test_flow_list_empty(self) -> None:
        lines, idx = _lines_and_idx("id: x\ngoals: []\n")
        val = lines[idx].split(":", 1)[1]
        assert _parse_goal_ids(val, lines, idx) == []

    def test_block_list_two_ids(self) -> None:
        text = "id: x\ngoals:\n  - goal-a\n  - goal-b\nstatus: active\n"
        lines, idx = _lines_and_idx(text)
        assert _parse_goal_ids("", lines, idx) == ["goal-a", "goal-b"]

    def test_flow_list_quoted_ids(self) -> None:
        lines, idx = _lines_and_idx('id: x\ngoals: ["goal-a", \'goal-b\']\n')
        val = lines[idx].split(":", 1)[1]
        assert _parse_goal_ids(val, lines, idx) == ["goal-a", "goal-b"]

    def test_block_list_quoted_ids(self) -> None:
        text = "id: x\ngoals:\n  - \"goal-a\"\n  - 'goal-b'\n"
        lines, idx = _lines_and_idx(text)
        assert _parse_goal_ids("", lines, idx) == ["goal-a", "goal-b"]

    def test_flow_list_whitespace_variance(self) -> None:
        lines, idx = _lines_and_idx("id: x\ngoals:   [  goal-a ,  goal-b  ]\n")
        val = lines[idx].split(":", 1)[1]
        assert _parse_goal_ids(val, lines, idx) == ["goal-a", "goal-b"]

    def test_bare_goals_key_zero_items(self) -> None:
        text = "id: x\ngoals:\nstatus: active\n"
        lines, idx = _lines_and_idx(text)
        assert _parse_goal_ids("", lines, idx) == []

    def test_indentation_boundary_sibling_field_not_slurped(self) -> None:
        """A `- x` line at <= the `goals:` key's own indentation is NOT consumed —
        proves the Finding-1 (initiativesummary-goals-join) indentation-boundary fix.
        Simulates a nested-under-a-different-key block list positioned right after a
        bare `goals:` key, at the SAME indentation as `goals:` itself (i.e. it is a
        sibling top-level scalar/list marker, not a child of `goals:`)."""
        text = "id: x\ngoals:\n- not-a-goal-id\nstatus: active\n"
        lines, idx = _lines_and_idx(text)
        # `goals:` is at column 0; the `- not-a-goal-id` line is also at column 0
        # (<=  goals_indent), so it must NOT be slurped.
        assert _parse_goal_ids("", lines, idx) == []


class TestUnquote:
    def test_double_quoted(self) -> None:
        assert _unquote('"goal-a"') == "goal-a"

    def test_single_quoted(self) -> None:
        assert _unquote("'goal-a'") == "goal-a"

    def test_unquoted_passthrough(self) -> None:
        assert _unquote("goal-a") == "goal-a"

    def test_whitespace_stripped(self) -> None:
        assert _unquote("  goal-a  ") == "goal-a"


# ---------------------------------------------------------------------------
# (a) new-field round-trip against the vendored 2.13.0 schema
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
def test_new_fields_round_trip_validates(tmp_path: Path) -> None:
    """A goal record carrying all 3 new 2.13.0 fields validates against the vendored pin."""
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record(
        weekly_perceptible=True,
        key_results_status=[
            {
                "id": "kr-1",
                "text": "Ship the thing",
                "kind": "milestone",
                "status": "on_track",
                "evidence_source": "manual",
                "weekly_perceptible": False,
            }
        ],
    )
    emitted = _emitted_goal(ctx, "test-host", raw)

    assert emitted["weekly_perceptible"] is True
    assert emitted["parent_goal_id"] is None
    assert emitted["key_results_status"] == [
        {"id": "kr-1", "text": "Ship the thing", "kind": "milestone", "status": "on_track"}
    ]

    validate_array([emitted], _ENTITY)


# ---------------------------------------------------------------------------
# (b) version-neutrality: old record with none of the new OPTIONAL keys still validates
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
def test_old_record_without_new_optional_keys_still_validates(tmp_path: Path) -> None:
    """A pre-2.13.0-shaped JSONL record (no weekly_perceptible/key_results_status) still
    validates.

    parent_goal_id is the one EXCEPTION to version-neutrality (now always-emitted); its
    present-as-null form is asserted to validate here (schema requires the key).
    """
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record()  # no weekly_perceptible, no key_results_status, no parent_goal_id
    emitted = _emitted_goal(ctx, "test-host", raw)

    assert "weekly_perceptible" not in emitted
    assert "key_results_status" not in emitted
    assert emitted["parent_goal_id"] is None

    validate_array([emitted], _ENTITY)


# ---------------------------------------------------------------------------
# (c) parent_goal_id always-present + present-as-null; omitted key rejects
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
class TestParentGoalIdAlwaysPresent:
    def test_parent_goal_id_key_always_present_on_emit(self, tmp_path: Path) -> None:
        """Even with no parent_goal_id in the raw JSONL, the emitted key IS present (as null)."""
        ctx = _make_ctx(tmp_path)
        raw = _base_goal_record()
        emitted = _emitted_goal(ctx, "test-host", raw)

        assert "parent_goal_id" in emitted
        assert emitted["parent_goal_id"] is None

    def test_parent_goal_id_non_null_passes_through(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        raw = _base_goal_record(parent_goal_id="goal-parent-001")
        emitted = _emitted_goal(ctx, "test-host", raw)

        assert emitted["parent_goal_id"] == "goal-parent-001"
        validate_array([emitted], _ENTITY)

    def test_fixture_record_with_parent_goal_id_key_omitted_fails_validation(
        self, tmp_path: Path
    ) -> None:
        """A hand-built wire record OMITTING parent_goal_id entirely fails Zod .strict()
        (the key is in the schema's `required` list — omission, not null, is the reject)."""
        bad_record = _base_goal_record(
            provenance={
                "source_kind": "coordinator_artifact",
                "repo": "test-org/test-repo",
                "ref": None,
                "path": "state/goals-log.test-host.jsonl",
                "observed_at": "2026-07-13T00:00:00Z",
                "derivation": "parsed",
            }
        )
        assert "parent_goal_id" not in bad_record

        with pytest.raises(ValidationError):
            validate_array([bad_record], _ENTITY)


# ---------------------------------------------------------------------------
# Provenance names the SURVIVING record's OWN shard, not the glob pattern
# ---------------------------------------------------------------------------

def test_provenance_path_names_each_records_own_shard_across_two_machines(
    tmp_path: Path,
) -> None:
    """Two per-machine shards each contribute one surviving (distinct goal_id) goal —
    each emitted record's provenance.path must name the CONCRETE shard it came from,
    never a shared value and never the glob pattern used to find the shards."""
    ctx = _make_ctx(tmp_path)
    _write_goal_log(
        ctx, "host-a", [_base_goal_record(goal_id="goal-from-host-a")]
    )
    _write_goal_log(
        ctx, "host-b", [_base_goal_record(goal_id="goal-from-host-b")]
    )

    records, malformed = goals_section.collect(ctx)

    assert malformed == []
    by_goal_id = {r["goal_id"]: r for r in records}
    assert set(by_goal_id) == {"goal-from-host-a", "goal-from-host-b"}
    assert (
        by_goal_id["goal-from-host-a"]["provenance"]["path"]
        == "state/goals-log.host-a.jsonl"
    )
    assert (
        by_goal_id["goal-from-host-b"]["provenance"]["path"]
        == "state/goals-log.host-b.jsonl"
    )


# ---------------------------------------------------------------------------
# (d) weekly_perceptible + key_results_status absent-when-absent
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("requires_vendor_pin")
def test_weekly_perceptible_and_key_results_status_absent_when_absent(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record()
    emitted = _emitted_goal(ctx, "test-host", raw)

    assert "weekly_perceptible" not in emitted
    assert "key_results_status" not in emitted
    validate_array([emitted], _ENTITY)


def test_weekly_perceptible_present_when_present(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record(weekly_perceptible=False)
    emitted = _emitted_goal(ctx, "test-host", raw)

    assert emitted["weekly_perceptible"] is False


def test_empty_key_results_list_omits_key_results_status(tmp_path: Path) -> None:
    """An empty key_results_status list on disk is falsy -> key_results_status omitted."""
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record(key_results_status=[])
    emitted = _emitted_goal(ctx, "test-host", raw)

    assert "key_results_status" not in emitted


# ---------------------------------------------------------------------------
# (e) key_results_status re-projection drops any stray evidence_source + per-KR
#     weekly_perceptible keys, keeping only {id, text, kind, status}
# ---------------------------------------------------------------------------

def test_key_results_projection_drops_evidence_source_and_weekly_perceptible(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record(
        key_results_status=[
            {
                "id": "kr-a",
                "text": "First KR",
                "kind": "metric",
                "status": "at_risk",
                "evidence_source": "github_graphql",
                "weekly_perceptible": True,
            },
            {
                "id": "kr-b",
                "text": "Second KR",
                "kind": "milestone",
                "status": "done",
                "evidence_source": "manual",
                "weekly_perceptible": False,
            },
        ]
    )
    emitted = _emitted_goal(ctx, "test-host", raw)

    assert emitted["key_results_status"] == [
        {"id": "kr-a", "text": "First KR", "kind": "metric", "status": "at_risk"},
        {"id": "kr-b", "text": "Second KR", "kind": "milestone", "status": "done"},
    ]
    for kr in emitted["key_results_status"]:
        assert "evidence_source" not in kr
        assert "weekly_perceptible" not in kr
        assert set(kr.keys()) == {"id", "text", "kind", "status"}


@pytest.mark.usefixtures("requires_vendor_pin")
def test_key_results_projection_validates_against_vendored_schema(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record(
        key_results_status=[
            {
                "id": "kr-a",
                "text": "First KR",
                "kind": "metric",
                "status": "at_risk",
                "evidence_source": "github_graphql",
                "weekly_perceptible": True,
            }
        ]
    )
    emitted = _emitted_goal(ctx, "test-host", raw)
    validate_array([emitted], _ENTITY)


@pytest.mark.usefixtures("requires_vendor_pin")
def test_key_results_status_item_missing_subfield_defaults_to_empty_string(
    tmp_path: Path,
) -> None:
    """A key_results_status item individually missing a required sub-field (e.g.
    `kind`) is emitted with that sub-field defaulted to "" — NOT quarantined/dropped —
    and this record VALIDATES SUCCESSFULLY against the schema.

    This is the actual production behavior, matching the upstream producer's contract
    (emit-goal-from-artifact.sh::_flush_kr(), which emits {id,text,kind,status} via
    `jq --arg` with "" for any individually-missing sub-field and only fail-loud-warns
    when ALL four are empty). Quarantining such items here would diverge from that
    producer contract and silently drop real KRs whose only defect is one blank
    sub-field.

    Review: code-reviewer (Finding 1) — replaces a prior version of this test that
    manually `del`d the key post-emission to force an artificial ValidationError,
    masking the real (validates-successfully) behavior."""
    ctx = _make_ctx(tmp_path)
    raw = _base_goal_record(
        key_results_status=[{"id": "kr-a", "text": "First KR", "status": "at_risk"}]  # kind omitted
    )
    emitted = _emitted_goal(ctx, "test-host", raw)
    assert emitted["key_results_status"] == [
        {"id": "kr-a", "text": "First KR", "kind": "", "status": "at_risk"}
    ]
    validate_array([emitted], _ENTITY)


# ---------------------------------------------------------------------------
# (f) InitiativeSummary.goals[] cross-join (_stamp_initiative_goals)
# ---------------------------------------------------------------------------

def _base_initiative_record(**overrides) -> dict:
    record = {
        "repo": "test-org/test-repo",
        "coordinator_root_path": ".",
        "id": "ini-2130",
        "label": "2.13.0 join fixture initiative",
        "provenance": {
            "source_kind": "coordinator_artifact",
            "repo": "test-org/test-repo",
            "ref": None,
            "path": "state/initiatives/ini-2130.yaml",
            "observed_at": "2026-07-13T00:00:00Z",
            "derivation": "parsed",
        },
        "owner": "test-em",
        "status": "active",
        "description": None,
    }
    record.update(overrides)
    return record




# ---------------------------------------------------------------------------
# (g) per-repo keying (FOLDS C4, plan AC6) — DR-025: a non-meta consumer repo emits its
#     OWN goals_current + initiatives records keyed on its own owner-qualified repo +
#     declared_by_machine anchor, with NO meta-repo-only gating.
# ---------------------------------------------------------------------------

