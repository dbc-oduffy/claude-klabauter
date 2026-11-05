"""Pins remote_safe on both sides: read-only ops pass, local-only and mutating ops refuse."""

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.remote_engine import policy


def _compute_op() -> str:
    return next(
        o
        for o, c in OP_CLASSIFICATION.items()
        if c is OpClass.COMPUTE_ONLY and not policy.is_local_only(o)
    )


def _mutating_op() -> str:
    return next(o for o, c in OP_CLASSIFICATION.items() if c is OpClass.MUTATING)


def test_compute_only_op_is_remote_safe():
    assert policy.remote_safe(_compute_op())
    assert policy.remote_safe("ping")


def test_mutating_op_refused_until_allowlisted(monkeypatch):
    op = _mutating_op()
    assert not policy.remote_safe(op)
    monkeypatch.setattr(policy, "PHASE2_MUTATING_ALLOWLIST", frozenset({op}))
    assert policy.remote_safe(op)


def test_unclassified_op_refused():
    assert not policy.remote_safe("no.such.op.anywhere")


def test_trim_never_remote_even_if_allowlisted(monkeypatch):
    for op in ("trim-file-cache", "BoxCacheTrim", "governor.view"):
        monkeypatch.setattr(policy, "PHASE2_MUTATING_ALLOWLIST", frozenset({op}))
        assert not policy.remote_safe(op)


def test_local_only_compute_op_refused(monkeypatch):
    op = _compute_op()
    monkeypatch.setattr(policy, "LOCAL_ONLY", frozenset({op}))
    assert not policy.remote_safe(op)


def test_phase2_allowlist_is_mint_land_only_and_classified_mutating():
    assert policy.PHASE2_MUTATING_ALLOWLIST == {
        "hooks.submit_engine_ask",
        "dispatch.emit",
    }
    assert not policy.remote_safe("dispatch.land_run")
    for op in policy.PHASE2_MUTATING_ALLOWLIST:
        assert OP_CLASSIFICATION[op] is OpClass.MUTATING
        assert policy.remote_safe(op)
        assert not policy.is_local_only(op)


def test_unlisted_mutating_op_still_refused():
    assert not policy.remote_safe("ceremony.commit_v2")
    assert not policy.remote_safe("dispatch.terminal_commit")


def test_far_result_indeterminate_both_sides():
    bad = {"ok": False, "state": "unknown", "reason": "timeout"}
    good = {"ok": True, "reply": {"a": 1}}
    assert policy.far_result_indeterminate("dispatch.emit", bad)
    assert policy.far_result_indeterminate("dispatch.emit", {"ok": True, "reply": "x"})
    assert policy.far_result_indeterminate("hooks.submit_engine_ask", None)
    assert not policy.far_result_indeterminate("dispatch.emit", good)
    assert not policy.far_result_indeterminate("ping", bad)
    assert policy.is_phase2_mutating("dispatch.emit")
    assert not policy.is_phase2_mutating("ping")


def test_carries_local_path_flags_path_keys_only():
    assert policy.carries_local_path({"cwd": "C" + ":\\w\\x"})
    assert policy.carries_local_path({"cwd": "E" + ":/w"})
    assert policy.carries_local_path({"p_path": "/abs"})
    assert policy.carries_local_path({"x": {"repo_root": "\\\\host\\s"}})
    assert policy.carries_local_path({"items": [{"out_dir": "/tmp/a"}]})
    assert not policy.carries_local_path({"n": 1})
    assert not policy.carries_local_path({"p": "/abs"})
    assert not policy.carries_local_path({"text": "/compact now"})
    assert not policy.carries_local_path({"cwd": "relative/dir", "prompt": "/x"})


def test_carries_local_path_plural_tilde_bare_drive():
    assert policy.carries_local_path({"paths": ["rel/a", "/abs/b"]})
    assert policy.carries_local_path({"files": ["ok", "C" + ":\\x"]})
    assert policy.carries_local_path({"target_paths": ["~/x"]})
    assert policy.carries_local_path({"cwd": "~"})
    assert policy.carries_local_path({"cwd": "E" + ":"})
    assert policy.carries_local_path({"out_dir": "~\\y"})
    assert not policy.carries_local_path({"paths": ["rel/a", "b/c"]})
    assert not policy.carries_local_path({"files": ["x.py"], "profiles": ["/abs"]})
    assert not policy.carries_local_path({"cwd": "~user", "path": "a:b"})
    assert not policy.carries_local_path({"notes": ["/abs", "E" + ":"]})


def test_reply_envelope():
    assert policy.reply_envelope("machine-a", 12) == {
        "served_by": "machine-a",
        "latency_ms": 12.0,
        "remote": True,
    }
