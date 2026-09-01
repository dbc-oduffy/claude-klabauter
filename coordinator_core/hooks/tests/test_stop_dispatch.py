"""
coordinator_core.hooks.tests.test_stop_dispatch — Tier-T test for the Stop
fan-in port (chunk C3, docs/reference/warm-hook-migration.md).

One op lands from this module: the composed fan-in (`hooks.stop_dispatch`).
The ported residue guard (`_guard_kira_verdict_routed_handler`) and three thin
wrappers over previously-handler-less library modules
(`_stop_em_report_altitude_handler`, `_nudge_harness_directive_dispatch_handler`,
`_nudge_unrouted_sizing_handler`) remain plain module-level functions the
fan-in calls directly — they lost their `@register_op` registrations
(overengineering-reviewer/Kira, 2026-08-31: no registration, dispatch site,
or cross-module caller found for any of the four op keys, in claude-klabauter or
DoE-claude) but are otherwise unchanged.

Obligations, per this chunk's dispatch brief:
  (a) `hooks.stop_dispatch` is registered and resolvable through
      `warm.hook_http.op_for_path`;
  (b) it is CLASSIFIED — an explicit assertion of the `classify()` result;
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


def test_stop_dispatch_op_registers_and_resolves_through_op_for_path() -> None:
    module = importlib.import_module(_MODULE)
    assert hasattr(module, "_handler")
    assert hasattr(module, "_guard_kira_verdict_routed_handler")
    assert hasattr(module, "_stop_em_report_altitude_handler")
    assert hasattr(module, "_nudge_harness_directive_dispatch_handler")
    assert hasattr(module, "_nudge_unrouted_sizing_handler")

    from coordinator_core.ipc import _REGISTRY

    for name in _OP_NAMES:
        assert name in _REGISTRY, name
        assert op_for_path(HOOK_PATH + "/" + name) == name

    # The four residue/wrapper functions above are deliberately NOT
    # registered (overengineering-reviewer finding, 2026-08-31): no
    # consumer names them as ops anywhere in claude-klabauter or DoE-claude.
    for name in (
        "hooks.guard_kira_verdict_routed",
        "hooks.stop_em_report_altitude",
        "hooks.nudge_harness_directive_dispatch",
        "hooks.nudge_unrouted_sizing",
    ):
        assert name not in _REGISTRY, name


def test_classification_matches_each_legs_write_semantics() -> None:
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


# --- CONCATENATE-ALL aggregation parity -------------------------------------
#
# The source dispatcher's contract is CONCATENATE-ALL, never first-fires-wins
# (`_stop_family_runner_contract.py`, and this module's own AGGREGATION
# CONTRACT docstring). A port that returned on the first firing leg would pass
# every single-leg test above and every no-signal test -- the divergence is
# only observable when TWO legs fire at once, which is why this asserts on the
# second leg's text specifically rather than on the first.


def _aggregate(payload):
    mod = importlib.import_module(_MODULE)
    handler = mod._handler
    result = handler({"payload": payload})
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return mod, result


def _silence_all_legs(monkeypatch, mod):
    """Every composed leg silent by default, so a test names exactly the legs
    it expects to fire rather than inheriting whatever the real legs decide."""
    for name in (
        "_runtime_tripwire_em_check_handler",
        "_watchdog_undischarged_next_move_handler",
        "_guard_kira_verdict_routed_handler",
        "_stop_em_report_altitude_handler",
        "_nudge_harness_directive_dispatch_handler",
        "_nudge_unrouted_sizing_handler",
    ):
        monkeypatch.setattr(mod, name, lambda _p: mod_no_advisory(), raising=True)


def mod_no_advisory():
    from coordinator_core.hooks._envelope import no_advisory

    return no_advisory()


def test_two_firing_advisory_legs_both_appear_not_just_the_first(monkeypatch):
    """CONCATENATE-ALL, advisory arm: both legs' text survives the fold."""
    from coordinator_core.hooks._envelope import post_advisory

    mod = importlib.import_module(_MODULE)
    _silence_all_legs(monkeypatch, mod)
    monkeypatch.setattr(
        mod, "_watchdog_undischarged_next_move_handler",
        lambda _p: post_advisory("ALPHA-LEG-TEXT"), raising=True)
    monkeypatch.setattr(
        mod, "_nudge_unrouted_sizing_handler",
        lambda _p: post_advisory("BRAVO-LEG-TEXT"), raising=True)

    # Review: coordinator:code-reviewer — session_id="" is load-bearing:
    # `_silence_all_legs` does not patch `_receiver_state_sensor_handler`,
    # so the real handler runs; a falsy session_id keeps it a no-op (see
    # that module's own "field(...) treats '' as absent" docstring). Do
    # not fill this in with a non-empty session_id without also patching
    # the sensor leg.
    _mod, result = _aggregate({"cwd": "", "session_id": "", "transcript_path": ""})
    blob = str(result)
    assert "ALPHA-LEG-TEXT" in blob, f"first firing leg lost: {result!r}"
    assert "BRAVO-LEG-TEXT" in blob, (
        f"second firing leg lost -- this is first-fires-wins, not the "
        f"CONCATENATE-ALL the source dispatcher contracts for: {result!r}"
    )


def test_two_blocking_legs_both_reasons_appear(monkeypatch):
    """CONCATENATE-ALL, block arm: a deny folds every reason, not the first."""
    from coordinator_core.hooks._envelope import deny

    mod = importlib.import_module(_MODULE)
    _silence_all_legs(monkeypatch, mod)
    monkeypatch.setattr(
        mod, "_guard_kira_verdict_routed_handler",
        lambda _p: deny("Stop", "BLOCK-REASON-ONE"), raising=True)
    monkeypatch.setattr(
        mod, "_runtime_tripwire_em_check_handler",
        lambda _p: deny("Stop", "BLOCK-REASON-TWO"), raising=True)

    # Review: coordinator:code-reviewer — session_id="" is load-bearing here
    # too, see the identical note in the advisory-arm test above.
    _mod, result = _aggregate({"cwd": "", "session_id": "", "transcript_path": ""})
    blob = str(result)
    assert "BLOCK-REASON-ONE" in blob and "BLOCK-REASON-TWO" in blob, (
        f"a deny must carry every blocking leg's reason, not the first: {result!r}"
    )
