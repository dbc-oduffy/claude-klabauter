"""sizing_acceptance reader helpers."""
import json
from pathlib import Path

from coordinator_core.ops.sizing_acceptance import (
    APM_ADMISSIBLE_MODES,
    acceptance_source,
    acceptance_words,
)

_SCHEMA = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "sizing-object.schema.json"

PM = {"pm_quote": "yes", "on": "2026-10-06", "mode": "pm"}
APM = {"source": "apm", "apm_ruling": "ruled", "on": "2026-10-06", "mode": "ceo"}


def test_words():
    assert acceptance_words(PM) == "yes"
    assert acceptance_words(APM) == "ruled"
    assert acceptance_words(None) is None
    assert acceptance_words({}) is None
    assert acceptance_words({"pm_quote": ""}) is None
    assert acceptance_words({"source": "apm", "pm_quote": "x"}) is None


def test_source():
    assert acceptance_source(PM) == "pm"
    assert acceptance_source(APM) == "apm"
    assert acceptance_source({"pm_quote": "x"}) == "pm"
    assert acceptance_source(None) is None
    assert acceptance_source({}) is None


def test_admissible_modes_match_the_schema_apm_mode_enum():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    accepted = schema["properties"]["exit_criterion"]["anyOf"][1]["properties"]["accepted"]
    apm_shape = next(s for s in accepted["anyOf"] if s.get("properties", {}).get("source"))
    assert tuple(apm_shape["properties"]["mode"]["enum"]) == APM_ADMISSIBLE_MODES
