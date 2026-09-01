"""
coordinator_core.hooks.tests.test_plan_persistence_check — Tier-T test for the
PostToolUse(ExitPlanMode) warm-door op.

Three obligations, per this chunk's dispatch brief (none catches the others):
  (a) the op is registered and resolvable through `warm.hook_http.op_for_path`;
  (b) it is CLASSIFIED — an explicit assertion of the `classify()` call/result,
      since routing alone never calls `_is_compute_only` for a prefixed op;
  (c) it returns the source script's shape for one real, firing payload.

Plus behavior coverage over the guard chain, the meta-repo reroute, and the
two write paths (routed via `plan_capture_persist.persist_captured_plan`, and
the raw-write fallback) — matching the source script's own three-way
collision/idempotent/persisted branch.
"""

from __future__ import annotations

import importlib

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path


def test_op_registers_and_resolves_through_op_for_path() -> None:
    module = importlib.import_module("coordinator_core.hooks.plan_persistence_check")
    assert hasattr(module, "_handler")

    from coordinator_core.ipc import _REGISTRY

    assert "hooks.plan_persistence_check" in _REGISTRY

    resolved = op_for_path(HOOK_PATH + "/hooks.plan_persistence_check")
    assert resolved == "hooks.plan_persistence_check"


def test_op_is_classified_mutating() -> None:
    result = classify("hooks.plan_persistence_check")
    assert result is OpClass.MUTATING


def _base_payload(cwd: str, plan: str = "# My Plan\n\nBody text.") -> dict:
    return {
        "tool_name": "ExitPlanMode",
        "tool_response": {"plan": plan, "isAgent": False},
        "cwd": cwd,
        "env": {},
    }


def _init_repo(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / "docs" / "plans").mkdir(parents=True)


def test_op_returns_post_advisory_shape_for_a_firing_payload(tmp_path, monkeypatch) -> None:
    from coordinator_core.hooks.plan_persistence_check import _handler
    from coordinator_core.ops import plan_capture_persist as pcp_mod

    repo = tmp_path / "repo"
    _init_repo(repo)

    # Review: coordinatorcode-reviewer.a986dd968d6771f99, Finding 2 — this is
    # the sole test in this file that lets the real routed path run, so it is
    # the one place a residual subprocess spawn (Finding 1) would otherwise
    # go undetected. Spy on the real subprocess.run call (not mocked out —
    # the scaffold write itself is still exercised) and pin that exactly one
    # spawn to coordinator-doc-new.py happens, so an eventual in-process port
    # of that scaffolder breaks this assertion loudly instead of silently.
    spawn_calls: list = []
    real_run = pcp_mod.subprocess.run

    def _spy_run(argv, *args, **kwargs):
        spawn_calls.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(pcp_mod.subprocess, "run", _spy_run)

    payload = _base_payload(str(repo))
    result = _handler({"payload": payload})

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "additionalContext" in hso
    assert "PLAN PERSISTED" in hso["additionalContext"] or "ALREADY PERSISTED" in hso["additionalContext"]

    # The routed path (plan_capture_persist.persist_captured_plan) is
    # preferred and succeeds here, scaffolding a schema-compliant artifact
    # rather than a raw dump — see module docstring's "Two write paths".
    written = list((repo / "docs" / "plans").glob("*.md"))
    assert len(written) == 1
    assert "My Plan" in written[0].read_text(encoding="utf-8")

    assert len(spawn_calls) == 1
    assert "coordinator-doc-new.py" in spawn_calls[0][1]


def test_op_suppresses_on_non_exitplanmode_tool_name() -> None:
    from coordinator_core.hooks.plan_persistence_check import _handler

    payload = {"tool_name": "Bash", "tool_response": {"plan": "# X"}, "cwd": "", "env": {}}
    assert _handler({"payload": payload}) == {}


def test_op_suppresses_on_is_agent_true() -> None:
    from coordinator_core.hooks.plan_persistence_check import _handler

    payload = {
        "tool_name": "ExitPlanMode",
        "tool_response": {"plan": "# X", "isAgent": True},
        "cwd": "",
        "env": {},
    }
    assert _handler({"payload": payload}) == {}


def test_op_suppresses_on_empty_plan() -> None:
    from coordinator_core.hooks.plan_persistence_check import _handler

    payload = {
        "tool_name": "ExitPlanMode",
        "tool_response": {"plan": "", "isAgent": False},
        "cwd": "",
        "env": {},
    }
    assert _handler({"payload": payload}) == {}


def test_op_suppresses_on_unresolvable_repo_root(tmp_path) -> None:
    from coordinator_core.hooks.plan_persistence_check import _handler

    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    payload = _base_payload(str(not_a_repo))
    assert _handler({"payload": payload}) == {}


def test_op_suppresses_when_docs_convention_absent(tmp_path) -> None:
    from coordinator_core.hooks.plan_persistence_check import _handler

    repo = tmp_path / "repo-no-docs"
    repo.mkdir()
    (repo / ".git").mkdir()
    payload = _base_payload(str(repo))
    assert _handler({"payload": payload}) == {}


def test_op_reads_claude_project_dir_from_payload_env_not_os_environ(tmp_path, monkeypatch) -> None:
    """CLAUDE_PROJECT_DIR must be read from params["payload"]["env"], never
    from this process's own os.environ — the payload-in contract this
    chunk's brief pins."""
    from coordinator_core.hooks.plan_persistence_check import _handler

    repo = tmp_path / "env-repo"
    _init_repo(repo)

    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    payload = {
        "tool_name": "ExitPlanMode",
        "tool_response": {"plan": "# Env Plan\n\nBody.", "isAgent": False},
        "cwd": "",
        "env": {"CLAUDE_PROJECT_DIR": str(repo)},
    }
    result = _handler({"payload": payload})
    assert result != {}
    written = list((repo / "docs" / "plans").glob("*.md"))
    assert len(written) == 1

    # And the inverse: an ambient os.environ CLAUDE_PROJECT_DIR must not be
    # consulted when the payload carries none.
    other_repo = tmp_path / "other-repo"
    _init_repo(other_repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other_repo))
    payload_no_env_dir = {
        "tool_name": "ExitPlanMode",
        "tool_response": {"plan": "# Ambient Plan\n\nBody.", "isAgent": False},
        "cwd": "",
        "env": {},
    }
    result2 = _handler({"payload": payload_no_env_dir})
    # No cwd and no payload env override -> unresolvable repo root -> no-op,
    # NOT routed to the ambient os.environ repo.
    assert result2 == {}
    assert list((other_repo / "docs" / "plans").glob("*.md")) == []


def test_op_readme_row_shape_matches_between_routed_and_fallback_paths(tmp_path, monkeypatch) -> None:
    """Review: coordinatorcode-reviewer.a986dd968d6771f99, Finding 5 — the
    routed path's README row (plan_capture_persist.readme_row()) and the
    fallback path's inline-built row must resolve to the same link-target
    shape for the same docs/README.md, since neither test previously
    asserted on the row TEXT (only file count/presence)."""
    import coordinator_core.hooks.plan_persistence_check as mod

    # --- Fallback path: force the routed path to error out. ---
    fallback_repo = tmp_path / "fallback-repo"
    _init_repo(fallback_repo)
    (fallback_repo / "docs" / "README.md").write_text("# Plans\n", encoding="utf-8")

    def _force_error(*args, **kwargs):
        return {"status": "error", "path": None, "readme_row": None, "reason": "forced"}

    monkeypatch.setattr(mod, "persist_captured_plan", _force_error)
    payload = _base_payload(str(fallback_repo), plan="# Fallback Plan\n\nBody.")
    result = mod._handler({"payload": payload})
    assert result != {}
    readme_text = (fallback_repo / "docs" / "README.md").read_text(encoding="utf-8")
    assert "](plans/2" in readme_text and "-fallback-plan.md)" in readme_text
    assert "](docs/plans/" not in readme_text

    # --- Routed path: let it run for real (no error-forcing). ---
    routed_repo = tmp_path / "routed-repo"
    _init_repo(routed_repo)
    (routed_repo / "docs" / "README.md").write_text("# Plans\n", encoding="utf-8")

    monkeypatch.undo()
    payload2 = _base_payload(str(routed_repo), plan="# Routed Plan\n\nBody.")
    result2 = mod._handler({"payload": payload2})
    assert result2 != {}
    readme_text2 = (routed_repo / "docs" / "README.md").read_text(encoding="utf-8")
    assert "](plans/2" in readme_text2 and "-routed-plan.md)" in readme_text2
    assert "](docs/plans/" not in readme_text2


def test_op_slug_collision_does_not_overwrite(tmp_path, monkeypatch) -> None:
    from coordinator_core.hooks.plan_persistence_check import _handler

    repo = tmp_path / "collision-repo"
    _init_repo(repo)

    # Force the routed path to report a non-collision error status so the
    # raw-write fallback (whose own collision check is under test) runs.
    def _force_error(*args, **kwargs):
        return {"status": "error", "path": None, "readme_row": None, "reason": "forced"}

    import coordinator_core.hooks.plan_persistence_check as mod

    monkeypatch.setattr(mod, "persist_captured_plan", _force_error)

    payload = _base_payload(str(repo), plan="# Collide\n\nFirst body.")
    first = mod._handler({"payload": payload})
    assert first != {}

    payload2 = _base_payload(str(repo), plan="# Collide\n\nDifferent body.")
    second = mod._handler({"payload": payload2})
    hso = second["hookSpecificOutput"]
    assert "collision" in hso["additionalContext"].lower()

    written = list((repo / "docs" / "plans").glob("*.md"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == "# Collide\n\nFirst body."


def test_op_idempotent_on_byte_identical_refire(tmp_path, monkeypatch) -> None:
    import coordinator_core.hooks.plan_persistence_check as mod

    repo = tmp_path / "idempotent-repo"
    _init_repo(repo)

    def _force_error(*args, **kwargs):
        return {"status": "error", "path": None, "readme_row": None, "reason": "forced"}

    monkeypatch.setattr(mod, "persist_captured_plan", _force_error)

    payload = _base_payload(str(repo), plan="# Same\n\nBody.")
    first = mod._handler({"payload": payload})
    assert "PLAN PERSISTED" in first["hookSpecificOutput"]["additionalContext"]

    second = mod._handler({"payload": payload})
    assert "ALREADY PERSISTED" in second["hookSpecificOutput"]["additionalContext"]


def test_op_routes_meta_repo_to_engine_root(tmp_path, monkeypatch) -> None:
    """A firing repo canonically equal to the resolved ~/.claude reroutes the
    write into this engine's own docs/plans/ (here, a fake substituted engine
    root), never the meta-repo's own docs/ tree."""
    import coordinator_core.hooks.plan_persistence_check as mod

    meta_repo = tmp_path / "dot-claude-home"
    (meta_repo / ".claude").mkdir(parents=True)
    (meta_repo / ".claude" / ".git").mkdir()

    fake_engine_root = tmp_path / "fake-engine"
    (fake_engine_root / "docs" / "plans").mkdir(parents=True)

    def _force_error(*args, **kwargs):
        return {"status": "error", "path": None, "readme_row": None, "reason": "forced"}

    monkeypatch.setattr(mod, "persist_captured_plan", _force_error)
    monkeypatch.setattr(mod, "_ENGINE_ROOT", fake_engine_root)

    payload = {
        "tool_name": "ExitPlanMode",
        "tool_response": {"plan": "# Meta Plan\n\nBody.", "isAgent": False},
        "cwd": str(meta_repo / ".claude"),
        "env": {"HOME": str(meta_repo)},
    }
    result = mod._handler({"payload": payload})
    assert result != {}
    assert not (meta_repo / ".claude" / "docs").exists()
    written = list((fake_engine_root / "docs" / "plans").glob("*meta-plan*.md"))
    assert len(written) == 1
