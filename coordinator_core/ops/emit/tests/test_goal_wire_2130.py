"""2.13.0 goal-wire coverage — new Goal fields + InitiativeSummary.goals[] join.

Covers plan § C5 items (a)-(g) for
docs/plans/2026-07-13-claude-klabauter-emit-goal-wire-2130-projection.md:

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
  (f) InitiativeSummary.goals[] cross-join (envelope._stamp_initiative_goals): populate,
      omit-on-unresolvable, and the claude-klabauter-live omit-on-every-initiative path; also that no
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
from coordinator_core.ops.emit.envelope import _stamp_initiative_goals
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


class TestInitiativeGoalsJoin:
    def test_populate_fixture_nests_full_goal_record(self, tmp_path: Path) -> None:
        """An initiative naming a resolvable goal-id gets the FULL Goal record nested."""
        goal_record = _base_goal_record(goal_id="goal-join-target")
        # Simulate the emitted shape (as goals.collect() would produce it).
        emitted_goal = dict(goal_record)
        emitted_goal["parent_goal_id"] = None
        emitted_goal["provenance"] = {
            "source_kind": "coordinator_artifact",
            "repo": "test-org/test-repo",
            "ref": None,
            "path": "state/goals-log.test-host.jsonl",
            "observed_at": "2026-07-13T00:00:00Z",
            "derivation": "parsed",
        }
        goals_current = [emitted_goal]

        initiative = _base_initiative_record(_goal_ids=["goal-join-target"])
        initiatives = [initiative]

        _stamp_initiative_goals(initiatives, goals_current)

        assert "_goal_ids" not in initiatives[0]
        assert initiatives[0]["goals"] == [emitted_goal]

    def test_omit_fixture_empty_goal_ids_omits_goals_key(self, tmp_path: Path) -> None:
        """An initiative with an empty goals: [] array omits the goals key entirely."""
        initiative = _base_initiative_record(_goal_ids=[])
        initiatives = [initiative]
        goals_current = [_base_goal_record(goal_id="unrelated-goal")]

        _stamp_initiative_goals(initiatives, goals_current)

        assert "_goal_ids" not in initiatives[0]
        assert "goals" not in initiatives[0]

    def test_omit_fixture_unresolvable_goal_id_omits_goals_key(self, tmp_path: Path) -> None:
        """A named goal-id that doesn't resolve in goals_current -> goals key omitted."""
        initiative = _base_initiative_record(_goal_ids=["goal-does-not-exist"])
        initiatives = [initiative]
        goals_current = [_base_goal_record(goal_id="some-other-goal")]

        _stamp_initiative_goals(initiatives, goals_current)

        assert "_goal_ids" not in initiatives[0]
        assert "goals" not in initiatives[0]

    def test_scope_qualified_resolution_same_id_different_repo_does_not_resolve(
        self, tmp_path: Path
    ) -> None:
        """A goal_id match in a DIFFERENT (repo, coordinator_root_path) scope does not resolve
        (goal ids are only unique within scope, per envelope._stamp_initiative_goals docstring)."""
        initiative = _base_initiative_record(_goal_ids=["goal-shared-id"])
        initiatives = [initiative]
        goals_current = [
            _base_goal_record(goal_id="goal-shared-id", repo="other-org/other-repo")
        ]

        _stamp_initiative_goals(initiatives, goals_current)

        assert "goals" not in initiatives[0]

    def test_live_state_omit_on_every_initiative_via_real_collect(self, tmp_path: Path) -> None:
        """Live-state omit path: initiatives.collect() on a real fixture with NO `goals:` key
        in the on-disk YAML produces `_goal_ids=[]` for every record, and after the enricher
        runs, every initiative omits `goals` and carries no leaked `_goal_ids` (claude-klabauter's live
        instances all have empty goals: -> omit-on-every-initiative, per plan context)."""
        ini_dir = tmp_path / "state" / "initiatives"
        ini_dir.mkdir(parents=True)
        (ini_dir / "live-ini-1.yaml").write_text(
            "id: live-ini-1\nlabel: Live initiative one\nstatus: active\n",
            encoding="utf-8",
        )
        (ini_dir / "live-ini-2.yaml").write_text(
            "id: live-ini-2\nlabel: Live initiative two\nstatus: paused\n",
            encoding="utf-8",
        )

        ctx = _make_ctx(tmp_path)
        records, malformed = initiatives_section.collect(ctx)
        assert malformed == []
        assert len(records) == 2

        goals_current: list[dict] = []
        _stamp_initiative_goals(records, goals_current)

        for record in records:
            assert "_goal_ids" not in record
            assert "goals" not in record

    def test_live_state_goals_id_array_resolves_via_real_collect(self, tmp_path: Path) -> None:
        """End-to-end (real on-disk YAML -> real collect() -> real enricher): an initiative
        YAML carrying ``goals: [<id>]`` (DR-207 ratified field), where ``<id>`` is a genuine
        ``goal_id`` present in ``goals_current`` (i.e. actually logged via ``goal.append`` /
        present in a ``goals-log.<machine>.jsonl`` shard, scoped to the same
        (repo, coordinator_root_path) as the initiative), emits a populated, resolved
        ``goals`` list on the InitiativeSummary record — not merely a non-null ``_goal_ids``
        staging key. Exercises ``initiatives_section.collect()`` and
        ``goals_section.collect()`` against real files, then the real ``_stamp_initiative_goals``
        join — no synthesized fixture dicts, unlike the ``_base_initiative_record`` tests above.
        """
        ctx = _make_ctx(tmp_path)

        ini_dir = tmp_path / "state" / "initiatives"
        ini_dir.mkdir(parents=True)
        (ini_dir / "live-ini-goals.yaml").write_text(
            "id: live-ini-goals\n"
            "label: Live initiative with a real goal attachment\n"
            "status: active\n"
            "goals:\n"
            "  - goal-join-target\n",
            encoding="utf-8",
        )

        _write_goal_log(
            ctx,
            "test-host",
            [_base_goal_record(goal_id="goal-join-target")],
        )

        ini_records, ini_malformed = initiatives_section.collect(ctx)
        assert ini_malformed == []
        assert len(ini_records) == 1
        assert ini_records[0]["_goal_ids"] == ["goal-join-target"]

        goal_records, goal_malformed = goals_section.collect(ctx)
        assert goal_malformed == []
        assert len(goal_records) == 1

        _stamp_initiative_goals(ini_records, goal_records)

        assert "_goal_ids" not in ini_records[0]
        assert "goals" in ini_records[0]
        assert len(ini_records[0]["goals"]) == 1
        assert ini_records[0]["goals"][0]["goal_id"] == "goal-join-target"
        assert ini_records[0]["goals"][0] == goal_records[0]

    def test_live_state_empty_goals_array_on_disk_omits_goals_key(self, tmp_path: Path) -> None:
        """An initiative YAML explicitly carrying ``goals: []`` on disk (the empty-array
        spelling, distinct from an omitted ``goals:`` key entirely — see
        ``state/initiatives/SCHEMA.md`` § ``goals`` field) still parses to an empty
        ``_goal_ids`` staging list, and the enricher omits the ``goals`` key from the wire
        record — the same absent-when-absent (D9 ``.optional()``) outcome as the
        key-omitted case covered by ``test_live_state_omit_on_every_initiative_via_real_collect``."""
        ctx = _make_ctx(tmp_path)

        ini_dir = tmp_path / "state" / "initiatives"
        ini_dir.mkdir(parents=True)
        (ini_dir / "live-ini-empty-goals.yaml").write_text(
            "id: live-ini-empty-goals\n"
            "label: Live initiative with an explicit empty goals array\n"
            "status: active\n"
            "goals: []\n",
            encoding="utf-8",
        )

        ini_records, ini_malformed = initiatives_section.collect(ctx)
        assert ini_malformed == []
        assert len(ini_records) == 1
        assert ini_records[0]["_goal_ids"] == []

        # A goal exists in scope, but must never attach — the initiative names no ids.
        goals_current = [_base_goal_record(goal_id="unrelated-live-goal")]
        _stamp_initiative_goals(ini_records, goals_current)

        assert "_goal_ids" not in ini_records[0]
        assert "goals" not in ini_records[0]


# ---------------------------------------------------------------------------
# (g) per-repo keying (FOLDS C4, plan AC6) — DR-025: a non-meta consumer repo emits its
#     OWN goals_current + initiatives records keyed on its own owner-qualified repo +
#     declared_by_machine anchor, with NO meta-repo-only gating.
# ---------------------------------------------------------------------------

class TestPerRepoKeying:
    """Mirrors test_per_repo_emission_integration.py's _make_ctx convention (DR-025)."""

    _SLUG_CONSUMER = "fixture-owner/consumer-repo"

    def test_non_meta_repo_emits_own_goal_record(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, repo_name=self._SLUG_CONSUMER)
        raw = _base_goal_record(
            goal_id="goal-consumer-001", repo=self._SLUG_CONSUMER, declared_by_machine="consumer-host"
        )
        emitted = _emitted_goal(ctx, "consumer-host", raw)

        assert emitted["repo"] == self._SLUG_CONSUMER
        assert emitted["declared_by_machine"] == "consumer-host"
        assert emitted["provenance"]["repo"] == self._SLUG_CONSUMER

    def test_non_meta_repo_initiative_keys_on_own_repo(self, tmp_path: Path) -> None:
        ini_dir = tmp_path / "state" / "initiatives"
        ini_dir.mkdir(parents=True)
        (ini_dir / "consumer-ini.yaml").write_text(
            "id: consumer-ini\nlabel: Consumer-owned initiative\nstatus: active\n",
            encoding="utf-8",
        )
        ctx = _make_ctx(tmp_path, repo_name=self._SLUG_CONSUMER)

        records, malformed = initiatives_section.collect(ctx)
        assert malformed == []
        assert len(records) == 1
        assert records[0]["repo"] == self._SLUG_CONSUMER
        assert records[0]["provenance"]["repo"] == self._SLUG_CONSUMER

    def test_no_meta_repo_gating_two_distinct_consumer_repos_do_not_collide(
        self, tmp_path: Path
    ) -> None:
        """Two distinct non-meta consumer repos emit distinct, non-colliding goal records —
        there is no hardcoded meta-repo-only gate suppressing or merging either."""
        repo_a_root = tmp_path / "repo-a"
        repo_b_root = tmp_path / "repo-b"
        ctx_a = _make_ctx(repo_a_root, repo_name="fixture-owner/repo-a")
        ctx_b = _make_ctx(repo_b_root, repo_name="fixture-owner/repo-b")

        _write_goal_log(
            ctx_a,
            "host-a",
            [_base_goal_record(goal_id="goal-a", repo="fixture-owner/repo-a",
                                declared_by_machine="host-a")],
        )
        _write_goal_log(
            ctx_b,
            "host-b",
            [_base_goal_record(goal_id="goal-b", repo="fixture-owner/repo-b",
                                declared_by_machine="host-b")],
        )

        records_a, _ = goals_section.collect(ctx_a)
        records_b, _ = goals_section.collect(ctx_b)

        assert len(records_a) == 1 and len(records_b) == 1
        assert records_a[0]["repo"] == "fixture-owner/repo-a"
        assert records_b[0]["repo"] == "fixture-owner/repo-b"
        assert records_a[0]["goal_id"] != records_b[0]["goal_id"]

    def test_coordinator_root_path_axis_does_not_collide(self, tmp_path: Path) -> None:
        """Two goals sharing `repo` and `goal_id` but differing `coordinator_root_path`
        are distinct per goals.py's grouping key (repo, coordinator_root_path, period,
        period_value) and do NOT collide as a single latest-wins record; the join in
        `_stamp_initiative_goals` also scopes on (repo, coordinator_root_path) and must
        resolve each initiative only against the goal in its own root.

        Review: code-reviewer (Finding 4, test-suite-c5 / Finding 4, initiativesummary-
        goals-join) — this axis was previously untested on both the per-repo-keying class
        and the join."""
        ctx = _make_ctx(tmp_path, repo_name=self._SLUG_CONSUMER)
        _write_goal_log(
            ctx,
            "consumer-host",
            [
                _base_goal_record(
                    goal_id="goal-shared-across-roots",
                    repo=self._SLUG_CONSUMER,
                    coordinator_root_path=".",
                    declared_by_machine="consumer-host",
                ),
                _base_goal_record(
                    goal_id="goal-shared-across-roots",
                    repo=self._SLUG_CONSUMER,
                    coordinator_root_path="./subroot",
                    declared_by_machine="consumer-host",
                ),
            ],
        )
        records, malformed = goals_section.collect(ctx)
        assert malformed == []
        # Both survive — no collision — because coordinator_root_path differs.
        assert len(records) == 2
        roots = {r["coordinator_root_path"] for r in records}
        assert roots == {".", "./subroot"}

        # Join-side: an initiative scoped to root "." only resolves the "." goal, not
        # the "./subroot" goal sharing the same goal_id + repo.
        initiative = _base_initiative_record(
            repo=self._SLUG_CONSUMER,
            coordinator_root_path=".",
            _goal_ids=["goal-shared-across-roots"],
        )
        initiatives = [initiative]
        _stamp_initiative_goals(initiatives, records)

        assert len(initiatives[0]["goals"]) == 1
        assert initiatives[0]["goals"][0]["coordinator_root_path"] == "."
