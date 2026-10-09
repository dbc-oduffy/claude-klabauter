"""
coordinator_core.ops.tests.test_sizing_accept_exit_criterion — the
"sizing.accept_exit_criterion" applier.

The property that matters most here is a negative one: this op writes exactly
`exit_criterion.accepted` (and, if given, `exit_criterion.statement`) — never
`pm_resolution`, `surfaced_to_pm`, `detents`, or `route`.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_accept_exit_criterion.py -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.sizing_accept_exit_criterion as accept_mod

# Declared, not excused: this file spawns a real process (git) because the op
# resolves its worktree root through git, which no fixture stands in for.
# Mirrors test_sizing_discharge_surfaced.py's own declaration --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_handler = accept_mod._handler

_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **_GIT_ENV},
        timeout=15, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _sizing_body(*, exit_criterion: str = "") -> str:
    lines = [
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        "status: routed",
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
    ]
    if exit_criterion:
        lines.append(exit_criterion.rstrip("\n"))
    return "\n".join(lines) + "\n"


def _seed_sizing(repo: Path, name: str = "20260101-a.yaml", **kwargs) -> Path:
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sizing_body(**kwargs), encoding="utf-8")
    return path


def _run(params: dict, repo: Path) -> dict:
    # `_handler` is a plain `def` (sync dispatch branch) — see its docstring.
    return _handler(params, repo_root=repo / ".git")


def _base(**overrides) -> dict:
    params = {
        "sizing": "state/sizings/20260101-a.yaml",
        "pm_quote": "Yes, that's the right bar.",
    }
    params.update(overrides)
    return params


_PROPOSED = "exit_criterion:\n  statement: Beat vanilla on category X.\n  accepted: null\n"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_accept_writes_accepted_and_keeps_statement(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)

    result = _run(_base(), repo)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["exit_criterion"]["statement"] == "Beat vanilla on category X."
    accepted = doc["exit_criterion"]["accepted"]
    assert accepted["pm_quote"] == "Yes, that's the right bar."
    assert accepted["mode"] == "hands-on"
    assert accepted["on"]


def test_accept_with_statement_replaces_the_proposed_one(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)

    result = _run(_base(statement="A sharper criterion the PM prefers."), repo)

    assert result["exit_code"] == 0, result
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["exit_criterion"]["statement"] == "A sharper criterion the PM prefers."


def test_accept_records_given_mode(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)

    result = _run(_base(mode="ceo"), repo)

    assert result["exit_code"] == 0, result
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["exit_criterion"]["accepted"]["mode"] == "ceo"
    assert doc["interaction_mode"] == "ceo"


def test_a_recorded_interaction_mode_is_never_overwritten(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED + "interaction_mode: pm\n")

    assert _run(_base(mode="ceo"), repo)["exit_code"] == 0

    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["interaction_mode"] == "pm"


def test_no_mode_given_records_no_interaction_mode(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)

    assert _run(_base(), repo)["exit_code"] == 0

    assert "interaction_mode" not in yaml.safe_load(sizing.read_text(encoding="utf-8"))


def test_other_fields_are_byte_identical(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    before = yaml.safe_load(sizing.read_text(encoding="utf-8"))

    assert _run(_base(), repo)["exit_code"] == 0

    after = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    for key in ("route", "detents", "status"):
        assert after[key] == before[key]
    assert "pm_resolution" not in after
    assert "surfaced_to_pm" not in after


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_missing_pm_quote_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    before = sizing.read_text(encoding="utf-8")

    result = _run(_base(pm_quote=""), repo)

    assert result["exit_code"] == 1
    assert "never composes or infers" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_no_statement_on_record_and_none_given_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo)  # no exit_criterion at all
    before = sizing.read_text(encoding="utf-8")

    result = _run(_base(), repo)

    assert result["exit_code"] == 1
    assert "no statement is on record" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_an_unknown_mode_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, exit_criterion=_PROPOSED)

    result = _run(_base(mode="boss"), repo)
    assert result["exit_code"] == 1
    assert "hands-on" in result["error"]


def test_a_second_acceptance_cannot_silently_displace_the_first(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    assert _run(_base(), repo)["exit_code"] == 0
    first = yaml.safe_load(sizing.read_text(encoding="utf-8"))["exit_criterion"]

    result = _run(_base(pm_quote="Actually, no — different bar."), repo)

    assert result["exit_code"] == 1
    assert "supersede" in result["error"]
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["exit_criterion"] == first


def test_supersede_replaces_the_prior_acceptance(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    assert _run(_base(), repo)["exit_code"] == 0

    result = _run(_base(pm_quote="Actually, no — different bar.", supersede=True), repo)

    assert result["exit_code"] == 0
    assert result["applied"] is True
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["exit_criterion"]["accepted"]["pm_quote"] == "Actually, no — different bar."


def test_a_path_outside_the_repo_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, exit_criterion=_PROPOSED)
    stray = repo / "README.md"

    result = _run(_base(sizing=str(stray)), repo)
    assert result["exit_code"] == 1
    assert "escapes" in result["error"]


def test_a_missing_sizing_file_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "state" / "sizings").mkdir(parents=True)

    result = _run(_base(sizing="state/sizings/nope.yaml"), repo)
    assert result["exit_code"] == 1
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_an_identical_acceptance_is_a_byte_identical_no_op(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    assert _run(_base(), repo)["exit_code"] == 0
    after_first = sizing.read_text(encoding="utf-8")

    result = _run(_base(), repo)

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert sizing.read_text(encoding="utf-8") == after_first


# ---------------------------------------------------------------------------
# Refusals always carry a reason
# ---------------------------------------------------------------------------


def test_amend_appends_keeps_prior_quote_and_validates(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    assert _run(_base(), repo)["exit_code"] == 0

    result = _run(_base(pm_quote="Amend it.", statement="A sharper bar.", mode="pm"), repo)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    ec = yaml.safe_load(sizing.read_text(encoding="utf-8"))["exit_criterion"]
    assert ec["statement"] == "A sharper bar."
    assert ec["accepted"]["pm_quote"] == "Yes, that's the right bar."
    assert len(ec["amendments"]) == 1
    am = ec["amendments"][0]
    assert (am["pm_quote"], am["statement"], am["mode"]) == ("Amend it.", "A sharper bar.", "pm")
    assert am["on"]
    assert accept_mod._validate_sizing_fm(yaml.safe_load(sizing.read_text(encoding="utf-8"))) == []


def test_a_second_amend_appends_again(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    assert _run(_base(), repo)["exit_code"] == 0
    assert _run(_base(pm_quote="One.", statement="Bar one."), repo)["exit_code"] == 0

    assert _run(_base(pm_quote="Two.", statement="Bar two."), repo)["exit_code"] == 0

    ec = yaml.safe_load(sizing.read_text(encoding="utf-8"))["exit_criterion"]
    assert ec["statement"] == "Bar two."
    assert [a["pm_quote"] for a in ec["amendments"]] == ["One.", "Two."]
    assert ec["accepted"]["pm_quote"] == "Yes, that's the right bar."


def test_post_mutation_schema_failure_propagates_its_message(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, exit_criterion=_PROPOSED)
    monkeypatch.setattr(
        accept_mod, "_validate_sizing_fm", lambda doc: [{"field": "exit_criterion", "error": "boom"}]
    )

    result = _run(_base(), repo)

    assert result["exit_code"] == 1
    assert "schema validation failed" in result["error"]
    assert "boom" in result["error"]


def test_an_unexpected_exception_still_yields_a_reason(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, exit_criterion=_PROPOSED)

    def explode(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(accept_mod, "locked_rmw", explode)

    result = _run(_base(), repo)

    assert result["exit_code"] == 1
    assert "disk on fire" in result["error"]


def test_missing_sizing_refusal_names_every_accepted_param():
    msg = _handler({"sizing_path": "state/sizings/x.yaml", "pm_quote": "q"})["error"]
    for name in ("sizing", "pm_quote", "apm_ruling", "ruling_ref", "statement", "mode", "supersede"):
        assert name in msg


# ---------------------------------------------------------------------------
# APM ruling
# ---------------------------------------------------------------------------

_CEO = "interaction_mode: ceo\n" + _PROPOSED
_APM_ACCEPTED = (
    "interaction_mode: ceo\nexit_criterion:\n  statement: Beat vanilla on category X.\n"
    "  accepted:\n    source: apm\n    apm_ruling: Ruled fine.\n    ruling_ref: run-1\n    on: '2026-01-01'\n"
    "    mode: ceo\n"
)
_PM_ACCEPTED = (
    "interaction_mode: ceo\nexit_criterion:\n  statement: Beat vanilla on category X.\n"
    "  accepted:\n    pm_quote: PM said yes.\n    on: '2026-01-01'\n    mode: ceo\n"
)


def _apm(**overrides) -> dict:
    params = {
        "sizing": "state/sizings/20260101-a.yaml",
        "apm_ruling": "Ruled fine.",
        "ruling_ref": "run-1",
    }
    params.update(overrides)
    return params


def _doc(sizing: Path) -> dict:
    return yaml.safe_load(sizing.read_text(encoding="utf-8"))


def _setup(tmp_path, body: str):
    repo = tmp_path / "repo"
    _init_repo(repo)
    return repo, _seed_sizing(repo, exit_criterion=body)


def test_apm_ruling_on_a_ceo_sizing_writes_the_apm_shape(tmp_path):
    repo, sizing = _setup(tmp_path, _CEO)
    result = _run(_apm(), repo)
    assert result["exit_code"] == 0 and result["applied"] is True, result
    accepted = _doc(sizing)["exit_criterion"]["accepted"]
    assert accepted["source"] == "apm" and accepted["apm_ruling"] == "Ruled fine."
    assert accepted["mode"] == "ceo" and "pm_quote" not in accepted


def test_apm_ruling_on_a_hands_on_sizing_is_refused_naming_hands_on(tmp_path):
    repo, _ = _setup(tmp_path, _PROPOSED)
    result = _run(_apm(), repo)
    assert result["exit_code"] == 1 and "hands-on" in result["error"]


def test_apm_ruling_mode_param_cannot_override_a_recorded_hands_on_mode(tmp_path):
    repo, sizing = _setup(tmp_path, "interaction_mode: hands-on\n" + _PROPOSED)
    before = sizing.read_text(encoding="utf-8")
    result = _run(_apm(mode="ceo"), repo)
    assert result["exit_code"] == 1 and "hands-on" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_both_or_neither_words_is_refused(tmp_path):
    repo, _ = _setup(tmp_path, _CEO)
    assert _run(_apm(pm_quote="q"), repo)["exit_code"] == 1
    assert _run({"sizing": "state/sizings/20260101-a.yaml"}, repo)["exit_code"] == 1


@pytest.mark.parametrize("supersede", [False, True])
def test_apm_ruling_never_displaces_a_pm_acceptance(tmp_path, supersede):
    repo, sizing = _setup(tmp_path, _PM_ACCEPTED)
    before = sizing.read_text(encoding="utf-8")
    result = _run(_apm(supersede=supersede, apm_ruling="Other."), repo)
    assert result["exit_code"] == 1
    assert sizing.read_text(encoding="utf-8") == before


def test_pm_quote_replaces_an_apm_acceptance_without_supersede(tmp_path):
    repo, sizing = _setup(tmp_path, _APM_ACCEPTED)
    result = _run(_base(pm_quote="PM overrules."), repo)
    assert result["exit_code"] == 0 and result["applied"] is True, result
    accepted = _doc(sizing)["exit_criterion"]["accepted"]
    assert accepted["pm_quote"] == "PM overrules." and accepted["source"] == "pm"
    assert accepted["history"] == [
        {"source": "apm", "apm_ruling": "Ruled fine.", "ruling_ref": "run-1", "on": "2026-01-01"}
    ]


def test_apm_without_ruling_ref_is_refused(tmp_path):
    repo, sizing = _setup(tmp_path, _CEO)
    before = sizing.read_text(encoding="utf-8")
    params = _apm()
    del params["ruling_ref"]
    result = _run(params, repo)
    assert result["exit_code"] == 1 and "ruling_ref" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_irreversible_apm_ruling_is_refused(tmp_path):
    repo, sizing = _setup(tmp_path, _CEO)
    before = sizing.read_text(encoding="utf-8")
    result = _run(_apm(apm_ruling="Approved; merge to main now."), repo)
    assert result["exit_code"] == 1 and "irreversible" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_apm_record_carries_ruling_ref(tmp_path):
    repo, sizing = _setup(tmp_path, _CEO)
    assert _run(_apm(), repo)["applied"] is True
    assert _doc(sizing)["exit_criterion"]["accepted"]["ruling_ref"] == "run-1"


def test_new_pm_record_carries_explicit_source(tmp_path):
    repo, sizing = _setup(tmp_path, _PROPOSED)
    assert _run(_base(), repo)["applied"] is True
    assert _doc(sizing)["exit_criterion"]["accepted"]["source"] == "pm"


def test_identical_apm_ruling_is_a_byte_identical_no_op(tmp_path):
    repo, sizing = _setup(tmp_path, _APM_ACCEPTED)
    before = sizing.read_text(encoding="utf-8")
    result = _run(_apm(), repo)
    assert result["exit_code"] == 0 and result["applied"] is False
    assert sizing.read_text(encoding="utf-8") == before


def test_omitted_mode_defaults_to_the_recorded_interaction_mode(tmp_path):
    repo, sizing = _setup(tmp_path, _CEO)
    assert _run(_base(), repo)["exit_code"] == 0
    assert _doc(sizing)["exit_criterion"]["accepted"]["mode"] == "ceo"

def test_accept_keeps_click_paths(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    proposed = (
        _PROPOSED
        + "  click_paths:\n  - role: admin\n    steps:\n    - Settings\n    - Grant access\n"
    )
    sizing = _seed_sizing(repo, exit_criterion=proposed)

    result = _run(_base(), repo)

    assert result["exit_code"] == 0, result
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["exit_criterion"]["click_paths"] == [
        {"role": "admin", "steps": ["Settings", "Grant access"]}
    ]
    assert doc["exit_criterion"]["accepted"]["pm_quote"]


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_statement_amendment_is_refused(tmp_path, blank):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, exit_criterion=_PROPOSED)
    before = sizing.read_text(encoding="utf-8")

    result = _run(_base(statement=blank), repo)

    assert result["exit_code"] == 1
    assert "statement is empty" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before
