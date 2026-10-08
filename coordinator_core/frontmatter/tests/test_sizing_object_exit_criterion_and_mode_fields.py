"""Schema coverage for sizing-object.schema.json 1.23.0's `exit_criterion` and
`interaction_mode` fields (pln-sizing-engine-carries-exit-cri-af770b § C1).

`exit_criterion` records the PM's primary success / exit criterion at sizing
time (target-design.md § 11): `{statement, accepted}`, where `accepted` is
either `null` (proposed, not yet accepted) or `{pm_quote, on, mode}` (the PM's
recorded acceptance, written only through `sizing.accept_exit_criterion`).
`interaction_mode` records which human touchpoints this sizing's mode commits
to (`hands-on`/`pm`/`ceo`). `detents[]` gains one appended value,
`exit_criterion_pending`.

NEGATIVE SPEC: this file asserts SHAPE only — it does not exercise
`sizing.accept_exit_criterion` (that op's own test file covers writing
`accepted`), `sizing_assemble`'s touchpoint table, or `coordinator-doc-new`'s
inheritance into a plan's `prime_exit_criterion`. Those are C4/C3/C6's own
test surfaces.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
_SIZING_OBJECT_SCHEMA = _SCHEMAS_DIR / "sizing-object.schema.json"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SIZINGS_DIR = _REPO_ROOT / "state" / "sizings"


def _minimal_sizing_object(**overrides) -> dict:
    fm = {
        "schema": "sizing-object",
        "intent": "Example PM ask, verbatim.",
        "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "routed",
        "premise": {"provenance": "unrecorded"},
    }
    fm.update(overrides)
    return fm


class TestExitCriterionField:
    def test_proposed_criterion_with_accepted_null_is_valid(self):
        fm = _minimal_sizing_object(
            exit_criterion={"statement": "Ship the M3 exit criterion", "accepted": None}
        )
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e["field"] == "exit_criterion" for e in errors), errors

    def test_accepted_criterion_with_pm_quote_on_mode_is_valid(self):
        fm = _minimal_sizing_object(
            exit_criterion={
                "statement": "Ship the M3 exit criterion",
                "accepted": {
                    "pm_quote": "Yes, that is the exit criterion.",
                    "on": "2026-09-27",
                    "mode": "pm",
                },
            }
        )
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e["field"] == "exit_criterion" for e in errors), errors

    def test_absent_exit_criterion_is_valid(self):
        # No backfill: the whole existing corpus has no exit_criterion.
        fm = _minimal_sizing_object()
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e["field"] == "exit_criterion" for e in errors), errors

    def test_null_exit_criterion_is_valid(self):
        fm = _minimal_sizing_object(exit_criterion=None)
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e["field"] == "exit_criterion" for e in errors), errors

    def test_empty_statement_is_rejected(self):
        fm = _minimal_sizing_object(exit_criterion={"statement": "", "accepted": None})
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e["field"] == "exit_criterion" for e in errors), errors

    def test_empty_pm_quote_is_rejected(self):
        fm = _minimal_sizing_object(
            exit_criterion={
                "statement": "Ship it",
                "accepted": {"pm_quote": "", "on": "2026-09-27", "mode": "pm"},
            }
        )
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e["field"] == "exit_criterion" for e in errors), errors

    def test_unknown_accepted_mode_is_rejected(self):
        fm = _minimal_sizing_object(
            exit_criterion={
                "statement": "Ship it",
                "accepted": {"pm_quote": "yes", "on": "2026-09-27", "mode": "dictator"},
            }
        )
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e["field"] == "exit_criterion" for e in errors), errors

    def test_extra_key_under_exit_criterion_is_rejected(self):
        fm = _minimal_sizing_object(
            exit_criterion={"statement": "Ship it", "accepted": None, "extra": "nope"}
        )
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e["field"] == "exit_criterion" for e in errors), errors

    def test_extra_key_under_accepted_is_rejected(self):
        fm = _minimal_sizing_object(
            exit_criterion={
                "statement": "Ship it",
                "accepted": {
                    "pm_quote": "yes",
                    "on": "2026-09-27",
                    "mode": "pm",
                    "extra": "nope",
                },
            }
        )
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e["field"] == "exit_criterion" for e in errors), errors


_APM = {"source": "apm", "apm_ruling": "x", "on": "2026-10-06", "mode": "ceo"}


def _ec_errors(accepted=None, amendments=None):
    ec = {"statement": "S", "accepted": accepted}
    if amendments is not None:
        ec["amendments"] = amendments
    fm = _minimal_sizing_object(exit_criterion=ec)
    return [e for e in validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA) if e["field"].startswith("exit_criterion")]


class TestApmAcceptanceShape:
    def test_apm_accepted_is_valid(self):
        assert not _ec_errors(_APM)

    def test_apm_with_run_id_is_valid(self):
        assert not _ec_errors({**_APM, "run_id": "r1"})

    def test_apm_with_pm_quote_is_rejected(self):
        assert _ec_errors({**_APM, "pm_quote": "q"})

    def test_source_pm_is_rejected(self):
        assert _ec_errors({**_APM, "source": "pm"})

    def test_empty_apm_ruling_is_rejected(self):
        assert _ec_errors({**_APM, "apm_ruling": ""})

    def test_apm_amendment_is_valid(self):
        assert not _ec_errors(_APM, [{**_APM, "statement": "T"}])

    def test_apm_amendment_with_pm_quote_is_rejected(self):
        assert _ec_errors(_APM, [{**_APM, "statement": "T", "pm_quote": "q"}])

    def test_apm_hands_on_acceptance_is_rejected(self):
        assert _ec_errors({**_APM, "mode": "hands-on"})

    def test_apm_hands_on_amendment_is_rejected(self):
        assert _ec_errors(_APM, [{**_APM, "mode": "hands-on", "statement": "T"}])


_ENGINE = {
    "source": "engine-size-rule", "rule": "engine-size-rule", "route": "plan",
    "tshirt": "M", "on": "2026-10-09", "mode": "ceo",
}


class TestEngineSizeRuleAcceptanceShape:
    @pytest.mark.parametrize("extra", [{}, {"route": "spec-dispatch", "tshirt": "XS"}, {"mode": "pm"}])
    def test_engine_record_is_valid(self, extra):
        assert not _ec_errors({**_ENGINE, **extra})

    def test_route_and_tshirt_are_optional(self):
        assert not _ec_errors({k: v for k, v in _ENGINE.items() if k not in ("route", "tshirt")})

    @pytest.mark.parametrize(
        "bad",
        [
            {"mode": "hands-on"},
            {"tshirt": "XL"},
            {"route": "shape"},
            {"rule": ""},
            {"pm_quote": "q"},
            {"extra": 1},
        ],
    )
    def test_engine_record_rejects(self, bad):
        assert _ec_errors({**_ENGINE, **bad})

    def test_engine_record_without_required_key_is_rejected(self):
        assert _ec_errors({k: v for k, v in _ENGINE.items() if k != "on"})

    def test_pm_apm_and_null_acceptances_still_validate(self):
        pm = {"pm_quote": "go", "on": "2026-10-01", "mode": "hands-on"}
        assert not _ec_errors(pm) and not _ec_errors(_APM) and not _ec_errors(None)


class TestInteractionModeField:
    @pytest.mark.parametrize("mode", ["hands-on", "pm", "ceo"])
    def test_each_mode_value_is_valid(self, mode):
        fm = _minimal_sizing_object(interaction_mode=mode)
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e["field"] == "interaction_mode" for e in errors), errors

    def test_absent_interaction_mode_is_valid(self):
        fm = _minimal_sizing_object()
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e["field"] == "interaction_mode" for e in errors), errors

    def test_unknown_mode_value_is_rejected(self):
        fm = _minimal_sizing_object(interaction_mode="dictator")
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e["field"] == "interaction_mode" for e in errors), errors


class TestExitCriterionPendingDetent:
    def test_exit_criterion_pending_detent_is_valid(self):
        fm = _minimal_sizing_object(detents=["exit_criterion_pending"])
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert not any(e["field"] == "detents" for e in errors), errors

    def test_unknown_detent_value_is_still_rejected(self):
        # Sanity check that the enum is still closed -- this bump only
        # appends, it does not open the array to arbitrary strings.
        fm = _minimal_sizing_object(detents=["not_a_real_detent"])
        errors = validate_frontmatter(fm, _SIZING_OBJECT_SCHEMA)
        assert any(e["field"].startswith("detents") for e in errors), errors


class TestExistingCorpusStillValidates:
    """AC: every record under state/sizings/ in THIS repo still validates.
    Corpus-walk shape reused from
    test_routed_sizing_carries_a_deliverable_id.py's `_iter_sizing_files`.

    NEGATIVE SPEC: this asserts NO NEW failures introduced by the 1.23.0 bump
    (exit_criterion/interaction_mode/exit_criterion_pending, and R7's
    first-person/repo_span removal) -- it does not assert a pristine corpus.
    The live corpus carries pre-existing, unrelated schema violations from
    other authoring sessions (additionalProperties drift, wrong-typed fields);
    those are a different defect with a different owner and this bump does
    not fix them. Same discipline the 1.15.0 bump note records ("three
    unrelated pre-existing failures remain and are NOT this field's")."""

    @staticmethod
    def _iter_sizing_files() -> list[Path]:
        if not _SIZINGS_DIR.is_dir():
            return []
        return sorted(_SIZINGS_DIR.glob("*.yaml"))

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_no_new_failures_introduced_by_this_bump(self):
        # Spawns one `git show` to read the pre-bump schema off HEAD; runs at
        # cadence gates, not per-commit. Spawn ratchet:
        # coordinator_core/tests/test_no_new_spawning_tests.py
        import subprocess

        from coordinator_core.win_portability import no_console_creationflags

        files = self._iter_sizing_files()
        if not files:
            pytest.skip("no state/sizings/*.yaml records found")

        old_bytes = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "show", "HEAD:coordinator_core/frontmatter/schemas/sizing-object.schema.json"],
            capture_output=True, text=True, timeout=30,
            **no_console_creationflags(),
        )
        if old_bytes.returncode != 0:
            pytest.skip("could not read the pre-bump schema from git HEAD")

        import json
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".schema.json", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(old_bytes.stdout)
            old_schema_path = Path(fh.name)

        try:
            regressions: list[str] = []
            checked = 0
            for path in files:
                try:
                    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    continue
                if not isinstance(doc, dict):
                    continue
                checked += 1
                new_errors = validate_frontmatter(doc, _SIZING_OBJECT_SCHEMA)
                if not new_errors:
                    continue
                old_errors = validate_frontmatter(doc, old_schema_path)
                if not old_errors:
                    regressions.append(f"{path.name}: {new_errors}")
            assert checked, "no sizing-object records found -- the corpus walk is not reaching state/sizings/"
            assert not regressions, (
                "sizing-object record(s) that validated before this bump now fail "
                "against the 1.23.0 schema (a real regression, not pre-existing drift):\n  "
                + "\n  ".join(regressions)
            )
        finally:
            old_schema_path.unlink(missing_ok=True)
