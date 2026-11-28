import jsonschema
import pytest

from coordinator_core.ops.repo_induct._contract import PARAMS_SCHEMA, make_step

GOOD = {"root": "root-fixture", "repo_key": "repos.player_preview", "project_type": "python"}


def test_good_params_validate():
    jsonschema.validate(GOOD, PARAMS_SCHEMA)


@pytest.mark.parametrize("key", ["player_preview", "repos.Player", "repos.", "repos.a-b", "repos.a.b"])
def test_bad_repo_key_refused(key):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**GOOD, "repo_key": key}, PARAMS_SCHEMA)


@pytest.mark.parametrize("missing", ["root", "repo_key", "project_type"])
def test_missing_required_refused(missing):
    params = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(params, PARAMS_SCHEMA)


def test_extra_property_refused():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**GOOD, "extra": 1}, PARAMS_SCHEMA)


def test_make_step_shape():
    assert make_step("a", "done") == {"step": "a", "status": "done", "detail": "", "changed": []}
    paths = ["p"]
    rec = make_step("b", "failed", "boom", paths)
    paths.append("q")
    assert rec["changed"] == ["p"] and rec["status"] == "failed" and rec["detail"] == "boom"
