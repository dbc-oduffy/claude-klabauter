"""
coordinator_core.ops.tests.test_sizing_discharge_surfaced — the
"sizing.discharge_surfaced" applier.

The property that matters most here is a negative one: a discharge records the
answer in `pm_resolution` and leaves the `surfaced_to_pm` entry byte-identical.
The schema's own negative spec says resolving an item by editing the array is the
failure the array exists to prevent, so an op that tidied the entry away would be
the defect, not the feature.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_discharge_surfaced.py -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.sizing_discharge_surfaced as discharge_mod

# Declared, not excused: this file spawns a real process (git) because the op
# resolves its worktree root through git, which no fixture stands in for.
# Mirrors test_sizing_decline.py's own declaration --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_handler = discharge_mod._handler

_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}

_ITEM = "Should the mirror be public before the OSS release, or after?"


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


def _sizing_body(*, surfaced: bool = True, pm_resolution: str = "") -> str:
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
    if surfaced:
        lines += [
            "surfaced_to_pm:",
            f"  - item: {_ITEM}",
            "    why_not_decided: External-facing and irreversible once published.",
            "  - item: Where should the wiki page live?",
            "    why_not_decided: Product direction, no correct answer from here.",
        ]
    else:
        lines.append("surfaced_to_pm: []")
    if pm_resolution:
        lines.append(pm_resolution.rstrip("\n"))
    return "\n".join(lines) + "\n"


def _seed_sizing(repo: Path, name: str = "20260101-a.yaml", **kwargs) -> Path:
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sizing_body(**kwargs), encoding="utf-8")
    return path


def _seed_dr(repo: Path, name: str = "DR-999-test.md") -> Path:
    path = repo / "docs" / "decisions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: test\n---\n\n# test\n", encoding="utf-8")
    return path


def _run(params: dict, repo: Path) -> dict:
    # `_handler` is a plain `def` (sync dispatch branch) — see its docstring.
    return _handler(params, repo_root=repo / ".git")


def _base(**overrides) -> dict:
    params = {
        "sizing_path": "state/sizings/20260101-a.yaml",
        "item": _ITEM,
        "resolution": "Public at release, not before — the mirror carries unreleased engine work.",
        "resolved_by": "docs/decisions/DR-999-test.md",
        "decided_on": "2026-09-11",
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_discharge_writes_pm_resolution(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo)
    _seed_dr(repo)

    result = _run(_base(), repo)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["pm_resolution"]["decided_on"] == "2026-09-11"
    key = result["key"]
    assert "not before" in doc["pm_resolution"][key]
    assert "DR-999-test.md" in doc["pm_resolution"][key]


def test_the_surfaced_entry_stays_listed_verbatim(tmp_path):
    """The schema's negative spec: an entry stays listed until its own artifact
    resolves it, and resolving it by editing the array is the failure the array
    exists to prevent. A discharge is recorded BESIDE the item, never instead of it.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo)
    _seed_dr(repo)
    before = yaml.safe_load(sizing.read_text(encoding="utf-8"))["surfaced_to_pm"]

    assert _run(_base(), repo)["exit_code"] == 0

    after = yaml.safe_load(sizing.read_text(encoding="utf-8"))["surfaced_to_pm"]
    assert after == before
    assert len(after) == 2


def test_a_substring_names_the_item(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo)
    _seed_dr(repo)

    result = _run(_base(item="mirror be public"), repo)
    assert result["exit_code"] == 0, result
    assert result["applied"] is True


def test_a_prior_resolution_under_another_key_survives(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(
        repo,
        pm_resolution="pm_resolution:\n  decided_on: '2026-08-01'\n  wiki_home: DoE, per the boundary doc.",
    )
    _seed_dr(repo)

    assert _run(_base(), repo)["exit_code"] == 0

    resolution = yaml.safe_load(sizing.read_text(encoding="utf-8"))["pm_resolution"]
    assert resolution["wiki_home"] == "DoE, per the boundary doc."
    assert resolution["decided_on"] == "2026-09-11"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_unresolvable_artifact_is_refused_before_any_write(tmp_path):
    """The live-evidence gate: a discharge no reader can follow is the YAML
    comment this field replaced."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo)
    before = sizing.read_text(encoding="utf-8")

    result = _run(_base(resolved_by="docs/decisions/DR-000-absent.md"), repo)

    assert result["exit_code"] == 1
    assert "real file" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_an_artifact_outside_the_allowed_roots_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo)
    stray = repo / "README.md"

    result = _run(_base(resolved_by=str(stray)), repo)
    assert result["exit_code"] == 1
    assert "escapes" in result["error"]


def test_an_ambiguous_item_is_refused_and_names_the_candidates(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo)
    _seed_dr(repo)

    result = _run(_base(item="the"), repo)
    assert result["exit_code"] == 1
    assert "matches 2 surfaced items" in result["error"]


def test_an_unmatched_item_lists_what_is_actually_surfaced(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo)
    _seed_dr(repo)

    result = _run(_base(item="a question nobody asked"), repo)
    assert result["exit_code"] == 1
    assert "no surfaced_to_pm item matches" in result["error"]
    assert "wiki page live" in result["error"]


def test_an_empty_surfaced_array_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, surfaced=False)
    _seed_dr(repo)

    result = _run(_base(), repo)
    assert result["exit_code"] == 1
    assert "no surfaced item here to answer" in result["error"]


def test_a_second_answer_cannot_silently_displace_the_first(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo)
    _seed_dr(repo)
    assert _run(_base(), repo)["exit_code"] == 0
    first = yaml.safe_load(sizing.read_text(encoding="utf-8"))["pm_resolution"]

    result = _run(_base(resolution="Actually publish it now."), repo)

    assert result["exit_code"] == 1
    assert "supersede" in result["error"]
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["pm_resolution"] == first


def test_supersede_replaces_the_prior_answer(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo)
    _seed_dr(repo)
    assert _run(_base(), repo)["exit_code"] == 0

    result = _run(_base(resolution="Actually publish it now.", supersede=True), repo)

    assert result["exit_code"] == 0
    assert result["applied"] is True
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert "publish it now" in doc["pm_resolution"][result["key"]]


def test_a_malformed_decided_on_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo)
    _seed_dr(repo)

    result = _run(_base(decided_on="11 September"), repo)
    assert result["exit_code"] == 1
    assert "YYYY-MM-DD" in result["error"]


def test_a_missing_resolution_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo)
    _seed_dr(repo)

    result = _run(_base(resolution=""), repo)
    assert result["exit_code"] == 1
    assert "does not compose the PM's reasoning" in result["error"]


def test_the_reserved_key_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo)
    _seed_dr(repo)

    result = _run(_base(key="decided_on"), repo)
    assert result["exit_code"] == 1
    assert "reserved key" in result["error"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_an_identical_discharge_is_a_byte_identical_no_op(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo)
    _seed_dr(repo)
    assert _run(_base(), repo)["exit_code"] == 0
    after_first = sizing.read_text(encoding="utf-8")

    result = _run(_base(), repo)

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert sizing.read_text(encoding="utf-8") == after_first


def test_the_key_is_derived_from_the_item_so_a_re_run_lands_in_one_place():
    assert discharge_mod.derive_key(_ITEM) == discharge_mod.derive_key(_ITEM)
    assert discharge_mod.derive_key(_ITEM) == "should_the_mirror_be_public_before"
    assert discharge_mod.derive_key("!!!") == "surfaced_item"
