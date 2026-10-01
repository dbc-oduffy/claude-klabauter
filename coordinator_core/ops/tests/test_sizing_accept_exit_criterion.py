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
