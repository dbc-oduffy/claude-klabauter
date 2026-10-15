"""Pins the bug-blitz contract: names, outcome/exit-code table, record path, schema validity."""

import json

import jsonschema
import pytest

from coordinator_core.bug_blitz import contract as c


def _schema():
    return json.loads(c.record_schema_path().read_text(encoding="utf-8"))


def _rec(outcome, **kw):
    base = dict(
        row_id="TF-1",
        runner="pytest",
        argv=("python", "-m", "pytest", "a/test_a.py"),
        selected=("a/test_a.py",),
        outcome=outcome,
        exit_code=0,
    )
    base.update(kw)
    return c.RunnerRecord(**base)


def test_names():
    assert c.SUBMIT_OP == "hooks.submit_bug_blitz"
    assert c.STUB_NAME == "bug-blitz"
    assert c.CLI_NAME == "targeted-test-run"
    assert c.RUN_TAG_PREFIX == "bug-blitz-"
    assert c.run_tag("r1") == "bug-blitz-r1"


def test_exit_codes_cover_outcomes_and_reserve_usage():
    assert set(c.EXIT_CODES) == set(c.OUTCOMES)
    assert [c.EXIT_CODES[o] for o in c.OUTCOMES] == [0, 1, 3, 4, 5]
    assert c.EXIT_USAGE == 2
    assert c.EXIT_USAGE not in c.EXIT_CODES.values()


def test_tests_record_rel():
    assert (
        c.tests_record_rel("bug", "run1", "TF-9")
        == "state/queue-grind/bug/evidence/run1/TF-9.tests.json"
    )


def test_schema_enum_matches_outcomes():
    assert tuple(_schema()["properties"]["outcome"]["enum"]) == c.OUTCOMES


@pytest.mark.parametrize(
    "rec",
    [
        _rec("passed"),
        _rec("failed", exit_code=1, failing_ids=("a/test_a.py::t",)),
        _rec("nothing-to-run", argv=(), selected=(), exit_code=None),
        _rec("refused", exit_code=None),
        _rec("no-runner", runner="", argv=(), exit_code=None),
    ],
    ids=lambda r: r.outcome,
)
def test_record_validates_for_each_outcome(rec):
    jsonschema.validate(rec.to_json(), _schema())


@pytest.mark.parametrize(
    "rec",
    [
        _rec("bogus"),
        _rec("passed", exit_code=None),
        _rec("refused", exit_code=0),
        _rec("nothing-to-run", exit_code=None),
        _rec("passed", selected=("a", "a")),
    ],
    ids=["bad-outcome", "passed-no-exit", "refused-with-exit", "nothing-with-selection", "dup-selected"],
)
def test_record_schema_rejects_inconsistent(rec):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(rec.to_json(), _schema())


def test_schema_rejects_extra_field():
    doc = _rec("passed").to_json()
    doc["extra"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, _schema())
