"""
coordinator_core/ops/tests/test_plan_prep_gate.py — the "plan.prep_gate" op.

Subject: `coordinator_core.ops.plan_prep_gate`, the read half of the mise-prep
certification pair. Two things are pinned here that the library's own suite
cannot pin: the eight-surface wire registration (an op missing one of them ships
present-but-dead, or silently degrades to `repo_root=None`), and the op's own
refusal-to-write.

The handler is synchronous (`03ed46b0e4` moved hook `_handler`s off `async def`) and
is called directly — this repo carries no pytest-asyncio dependency.

Zero spawns. `locked_rmw` never runs on this path and no case needs a real git
repo, so every fixture is a bare `tmp_path` tree with a `.git` DIRECTORY and no
`git init`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.benchmarks.budget import resolve_budget
from coordinator_core.ops import plan_prep_gate as mod
from coordinator_core.roadmap import prep_gate as pg

OP_KEY = "plan.prep_gate"


def _gate(params: dict, repo_root: Path) -> dict:
    return mod._handler(params, repo_root)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "coordinator_core").mkdir()
    return tmp_path / ".git"


_FM = """census: []
prime_exit_criterion:
  statement: the op reports per class
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _plan(root: Path, *, frontmatter: str = _FM, spine: str | None = _SPINE) -> str:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    body = ["", "# Fixture", ""]
    if spine is not None:
        body += ["## Tasks", "", "```yaml plan-tasks", spine.strip(), "```", ""]
    (plans / "2026-09-07-fixture.md").write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-09-07\n"
        + frontmatter
        + "---\n"
        + "\n".join(body),
        encoding="utf-8",
    )
    return "docs/plans/2026-09-07-fixture.md"


def test_op_resolves_through_the_real_dispatch_path():
    """`_lazy_import_and_lookup` is the path `coordinator-invoke` actually takes
    on a registry miss (OP_MODULE_MAP targeted import, then the `_eager_import_all`
    safe fallback). Raw `_REGISTRY` membership would test pytest collection order
    against a lazy package, not wire registration."""
    handler = ipc._REGISTRY.get(OP_KEY) or ipc._lazy_import_and_lookup(OP_KEY)
    assert callable(handler), f"{OP_KEY!r} ships present-but-dead"


def test_op_is_classified_compute_only():
    assert classify(OP_KEY) is OpClass.COMPUTE_ONLY


def test_op_is_keyed_common_dir():
    """A missing `_OP_KEY_SCOPE` entry is not an error — it silently degrades to
    scope "none" and the handler is handed `repo_root=None`."""
    assert ipc.OP_KEY_SCOPE.get(OP_KEY) == "common_dir"


def test_op_resolves_a_budget():
    assert resolve_budget(OP_KEY, classify(OP_KEY))["target_ms"] > 0


def test_the_op_declares_it_generates_nothing():
    assert mod.GENERATES == []
    assert not hasattr(mod, "MUTATES")


def test_absent_repo_root_refuses_rather_than_falling_back_to_cwd(tmp_path):
    with pytest.raises(ValueError, match="requires a resolved repo_root"):
        mod._handler({"plan": "docs/plans/x.md"}, None)


@pytest.mark.parametrize("bad", [None, "", "   ", 7, ["a"]])
def test_plan_must_name_one_plan(tmp_path, bad):
    common = _repo(tmp_path)
    with pytest.raises(ValueError, match=r"plan \(or plan_path\) must be a non-empty string"):
        _gate({"plan": bad}, common)


def test_a_plan_outside_the_worktree_is_refused(tmp_path):
    common = _repo(tmp_path)
    outside = tmp_path.parent / "elsewhere.md"
    outside.write_text("---\ntitle: x\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the resolved worktree"):
        _gate({"plan": str(outside)}, common)


def test_a_missing_plan_is_refused_by_name(tmp_path):
    common = _repo(tmp_path)
    with pytest.raises(ValueError, match="no such plan"):
        _gate({"plan": "docs/plans/nope.md"}, common)


def test_a_clean_plan_reports_prepped_with_every_class(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    result = _gate({"plan": rel}, common)
    assert result["plan"] == rel
    assert result["verdict"] == pg.PREPPED
    assert tuple(result["classes"]) == pg.CLASS_ORDER
    assert result["withheld_rows"] == []


def test_the_report_is_per_class_not_a_boolean(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter="", spine=None)
    result = _gate({"plan": rel}, common)
    assert result["verdict"] == pg.NOT_PREPPED
    failing = {k for k, v in result["classes"].items() if v["status"] != "PASS"}
    assert failing == {"SPINE", "CENSUS", "PRIME_EXIT"}
    assert result["classes"]["EXTERNAL_DEPS"]["status"] == "PASS"


def test_the_report_carries_the_stamp_state(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    result = _gate({"plan": rel}, common)
    assert result["stamp"]["state"] == pg.UNSTAMPED
    assert result["stamp"]["body_sha"]


def test_the_op_writes_nothing(tmp_path):
    """A closed gate is REPORTED here; refusing on it is the caller's act and
    stamping on it is plan.stamp_prepped's."""
    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    path = tmp_path / rel
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    result = _gate({"plan": rel}, common)
    assert result["verdict"] == pg.PREPPED
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_the_op_never_spawns_a_subprocess(tmp_path, monkeypatch):
    import subprocess

    from coordinator_core import engine_version

    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    _gate({"plan": rel}, common)
    engine_version._BUILD_MEMO = None

    def _boom(*args, **kwargs):
        raise AssertionError("plan.prep_gate must not create a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert _gate({"plan": rel}, common)["verdict"] == pg.PREPPED


def test_the_report_names_the_build_that_computed_it(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    report = _gate({"plan": rel}, common)
    assert report["verdict"] == pg.PREPPED
    assert set(report["engine_build"]) == {"engine_sha", "engine_dirty"}


def test_the_build_is_the_engines_own_not_the_gated_repos(tmp_path):
    from coordinator_core import engine_version

    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    engine_version._BUILD_MEMO = None
    report = _gate({"plan": rel}, common)
    assert report["engine_build"]["engine_sha"] == engine_version.engine_build()["engine_sha"]


def test_plan_path_is_accepted_as_the_plan_spelling(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    assert _gate({"plan_path": rel}, common)["plan"] == rel


def test_disagreeing_plan_spellings_are_refused(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path)
    with pytest.raises(ValueError, match="plan and plan_path"):
        _gate({"plan": rel, "plan_path": "docs/plans/other.md"}, common)
