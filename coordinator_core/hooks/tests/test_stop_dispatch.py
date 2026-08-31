"""
coordinator_core.hooks.tests.test_stop_dispatch — Tier-T test for the Stop
fan-in port (chunk C3, docs/reference/warm-hook-migration.md).

Five ops land from this one module: the composed fan-in (`hooks.stop_dispatch`),
the ported residue guard (`hooks.guard_kira_verdict_routed`), and three thin
wrappers over previously-handler-less library modules
(`hooks.stop_em_report_altitude`, `hooks.nudge_harness_directive_dispatch`,
`hooks.nudge_unrouted_sizing`).

Obligations, per this chunk's dispatch brief:
  (a) all five ops are registered and resolvable through
      `warm.hook_http.op_for_path`;
  (b) each is CLASSIFIED — an explicit assertion of the `classify()` result;
  (c) `guard_kira_verdict_routed` reproduces `guard-kira-verdict-routed.py`'s
      own decision against a captured payload/sidecar corpus (trigger-scope
      skip, clean pass, and the unrouted-verdict BLOCK);
  (d) the aggregate `hooks.stop_dispatch` composes without raising on a
      minimal no-signal Stop payload.
"""

from __future__ import annotations

import asyncio
import importlib
import os

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path

_MODULE = "coordinator_core.hooks.stop_dispatch"

_OP_NAMES = (
    "hooks.stop_dispatch",
    "hooks.guard_kira_verdict_routed",
    "hooks.stop_em_report_altitude",
    "hooks.nudge_harness_directive_dispatch",
    "hooks.nudge_unrouted_sizing",
)


def _make_repo(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    return str(repo_root)


def _share_dir(repo_root: str, session_id: str) -> str:
    path = os.path.join(repo_root, "state", "subagent-share", session_id)
    os.makedirs(path, exist_ok=True)
    return path


def _write_sidecar(share_dir: str, filename: str, frontmatter_lines: list) -> None:
    body = "---\n" + "\n".join(frontmatter_lines) + "\n---\n\n## body\n"
    with open(os.path.join(share_dir, filename), "w", encoding="utf-8") as fh:
        fh.write(body)


def test_all_five_ops_register_and_resolve_through_op_for_path() -> None:
    module = importlib.import_module(_MODULE)
    assert hasattr(module, "_handler")
    assert hasattr(module, "_guard_kira_verdict_routed_handler")

    from coordinator_core.ipc import _REGISTRY

    for name in _OP_NAMES:
        assert name in _REGISTRY, name
        assert op_for_path(HOOK_PATH + "/" + name) == name


def test_classification_matches_each_legs_write_semantics() -> None:
    assert classify("hooks.guard_kira_verdict_routed") == OpClass.COMPUTE_ONLY
    assert classify("hooks.stop_em_report_altitude") == OpClass.MUTATING
    assert classify("hooks.nudge_harness_directive_dispatch") == OpClass.MUTATING
    assert classify("hooks.nudge_unrouted_sizing") == OpClass.MUTATING
    assert classify("hooks.stop_dispatch") == OpClass.MUTATING


def test_guard_kira_verdict_routed_skips_on_subagent_stop(tmp_path) -> None:
    from coordinator_core.hooks.stop_dispatch import _guard_kira_verdict_routed

    repo_root = _make_repo(tmp_path)
    _share_dir(repo_root, "sess-1")
    payload = {"cwd": repo_root, "session_id": "sess-1", "agent_id": "some-subagent"}

    assert _guard_kira_verdict_routed(payload) == {}


def test_guard_kira_verdict_routed_skips_on_stop_hook_active(tmp_path) -> None:
    from coordinator_core.hooks.stop_dispatch import _guard_kira_verdict_routed

    repo_root = _make_repo(tmp_path)
    _share_dir(repo_root, "sess-1")
    payload = {"cwd": repo_root, "session_id": "sess-1", "stop_hook_active": True}

    assert _guard_kira_verdict_routed(payload) == {}


def test_guard_kira_verdict_routed_clean_pass_no_sidecars(tmp_path) -> None:
    from coordinator_core.hooks.stop_dispatch import _guard_kira_verdict_routed

    repo_root = _make_repo(tmp_path)
    _share_dir(repo_root, "sess-1")
    payload = {"cwd": repo_root, "session_id": "sess-1"}

    assert _guard_kira_verdict_routed(payload) == {}


def test_guard_kira_verdict_routed_blocks_unanswered_findings(tmp_path) -> None:
    from coordinator_core.hooks.stop_dispatch import _guard_kira_verdict_routed

    repo_root = _make_repo(tmp_path)
    share_dir = _share_dir(repo_root, "sess-1")
    _write_sidecar(
        share_dir,
        "coordinatoroverengineering-reviewer.abc.md",
        [
            "agent_type: coordinator:overengineering-reviewer",
            "spawned_at: 2026-08-31T00:00:00Z",
            "findings_count: 2",
        ],
    )
    payload = {"cwd": repo_root, "session_id": "sess-1"}

    result = _guard_kira_verdict_routed(payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "unrouted Kira" in reason
    assert "findings_count=2" in reason


def test_guard_kira_verdict_routed_passes_when_answered(tmp_path) -> None:
    from coordinator_core.hooks.stop_dispatch import _guard_kira_verdict_routed

    repo_root = _make_repo(tmp_path)
    share_dir = _share_dir(repo_root, "sess-1")
    kira_file = "coordinatoroverengineering-reviewer.abc.md"
    _write_sidecar(
        share_dir,
        kira_file,
        [
            "agent_type: coordinator:overengineering-reviewer",
            "spawned_at: 2026-08-31T00:00:00Z",
            "findings_count: 2",
        ],
    )
    _write_sidecar(
        share_dir,
        "coordinatorreview-integrator.def.md",
        [
            "agent_type: coordinator:review-integrator",
            "spawned_at: 2026-08-31T00:01:00Z",
            "integrated_from: [coordinatoroverengineering-reviewer.abc]",
        ],
    )
    payload = {"cwd": repo_root, "session_id": "sess-1"}

    assert _guard_kira_verdict_routed(payload) == {}


def test_stop_em_report_altitude_wrapper_suppresses_on_subagent_stop() -> None:
    from coordinator_core.hooks.stop_dispatch import _stop_em_report_altitude_handler

    payload = {"agent_id": "some-subagent"}
    result = _stop_em_report_altitude_handler({"payload": payload})
    assert result == {}


def test_aggregate_stop_dispatch_no_signal_returns_no_advisory(tmp_path) -> None:
    from coordinator_core.hooks.stop_dispatch import _handler

    repo_root = _make_repo(tmp_path)
    _share_dir(repo_root, "sess-1")
    payload = {"cwd": repo_root, "session_id": "sess-1", "agent_id": "some-subagent"}

    result = asyncio.run(_handler({"payload": payload}))
    assert result == {}


def test_aggregate_stop_dispatch_folds_kira_block(tmp_path) -> None:
    from coordinator_core.hooks.stop_dispatch import _handler

    repo_root = _make_repo(tmp_path)
    share_dir = _share_dir(repo_root, "sess-1")
    _write_sidecar(
        share_dir,
        "coordinatoroverengineering-reviewer.abc.md",
        [
            "agent_type: coordinator:overengineering-reviewer",
            "spawned_at: 2026-08-31T00:00:00Z",
            "findings_count: 1",
        ],
    )
    payload = {"cwd": repo_root, "session_id": "sess-1"}

    result = asyncio.run(_handler({"payload": payload}))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "unrouted Kira" in result["hookSpecificOutput"]["permissionDecisionReason"]
