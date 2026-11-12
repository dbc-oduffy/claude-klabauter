"""Contract tests for the example-retrieval-repo usage/probe adapter."""

import json

from coordinator_core.ops.dispatch_emit import rag_use
from coordinator_core.ops.dispatch_emit.rag_use import ProbeState, RagUsage


def _runner(payload, seen=None):
    def run(argv):
        if seen is not None:
            seen.append(argv)
        return payload if isinstance(payload, str) or payload is None else json.dumps(payload)

    return run


def test_usage_parses_and_builds_argv():
    seen = []
    u = rag_use.read_usage(
        "s1", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
        runner=_runner({"calls": {"semantic": 2, "symbol": 3}, "timeouts": 1, "degraded": True}, seen),
    )
    assert u == RagUsage(calls=5, by_tool={"semantic": 2, "symbol": 3}, timeouts=1, degraded=True)
    assert seen[0] == ["project-rag", "usage", "--session", "s1", "--since",
                       "2026-01-01T00:00:00Z", "--until", "2026-01-01T01:00:00Z", "--json"]


def test_usage_int_calls_and_zero():
    u = rag_use.read_usage("s", "a", "b", runner=_runner({"calls": 0}))
    assert u is not None and u.calls == 0 and u.by_tool == {} and not u.degraded
    u = rag_use.read_usage("s", "a", "b", runner=_runner({"calls": 7, "by_tool": {"x": 7}}))
    assert u.calls == 7 and u.by_tool == {"x": 7}


def test_usage_unreadable_is_none_not_zero():
    assert rag_use.read_usage("s", "a", "b", runner=_runner(None)) is None
    assert rag_use.read_usage("s", "a", "b", runner=_runner("not json")) is None
    assert rag_use.read_usage("s", "a", "b", runner=_runner({"other": 1})) is None


def test_probe_parses_each_state_and_refuses_unknown():
    for s in ("ok", "degraded", "down"):
        p = rag_use.probe(runner=_runner({"state": s, "per_source": {"engine": s}}))
        assert p == ProbeState(state=s, per_source={"engine": s})
    assert rag_use.probe(runner=_runner({"state": "weird"})) is None
    assert rag_use.probe(runner=_runner(None)) is None


def test_no_runner_is_none():
    assert rag_use.probe() is None
    assert rag_use.read_usage("s", "a", "b") is None
    assert rag_use.read_usage("s", "a", "b", runner=None) is None


def test_is_ue_repo_uses_find_uproject(tmp_path):
    from coordinator_core.ops.dispatch_emit import ue_route_source

    ue_route_source.clear_caches()
    assert rag_use.is_ue_repo(tmp_path) is False
    ue_route_source.clear_caches()
    (tmp_path / "Game.uproject").write_text("{}")
    assert rag_use.is_ue_repo(tmp_path) is True


def test_is_ue_repo_true_for_declared_project_type_without_uproject(tmp_path):
    from coordinator_core.ops.dispatch_emit import ue_route_source

    ue_route_source.clear_caches()
    (tmp_path / "coordinator.local.md").write_text(
        "---\nproject_type: unreal-engine\n---\n", encoding="utf-8"
    )
    assert rag_use.is_ue_repo(tmp_path) is True
    ue_route_source.clear_caches()


def _verdict(**kw):
    base = dict(ue=True, rag_installed=True, rag_calls=3, rag_failed=0)
    base.update(kw)
    return rag_use.docs_check_rag_verdict(**base)


def test_verdict_zero_calls_is_rag_not_used_never_down():
    assert _verdict(rag_calls=0) == "rag_not_used"
    assert _verdict(rag_calls=0, engine_queryable=True) == "rag_not_used"
    assert rag_use.RAG_NOT_USED != rag_use.HALT_RAG_CORPUS_DOWN


def test_verdict_down_when_all_refused_else_degraded():
    assert _verdict(rag_calls=4, rag_failed=4, rag_refused=4) == "rag_corpus_down"
    assert _verdict(rag_calls=4, rag_failed=4) == "degraded"


def test_verdict_timeout_only_is_degraded_not_down():
    assert _verdict(rag_calls=4, rag_failed=4, rag_timeouts=4) == "degraded"
    assert _verdict(rag_calls=4, rag_failed=1, rag_timeouts=1) == "degraded"


def test_verdict_failure_while_queryable_is_degraded():
    assert _verdict(rag_calls=2, rag_failed=2, engine_queryable=True) == "degraded"


def test_verdict_definite_down_still_halts():
    assert _verdict(rag_calls=3, rag_failed=3, rag_refused=3) == "rag_corpus_down"
    assert _verdict(rag_calls=3, rag_failed=3, rag_timeouts=2, rag_refused=1) == "rag_corpus_down"
    assert _verdict(rag_calls=3, rag_failed=1, rag_timeouts=1, engine_queryable=False) == "rag_corpus_down"
    assert _verdict(rag_calls=0, engine_queryable=False) == "rag_corpus_down"


def test_verdict_ok_on_any_success():
    assert _verdict() == "ok"
    assert _verdict(rag_calls=4, rag_failed=3) == "ok"


def test_verdict_not_applicable_off_ue_or_without_rag():
    assert _verdict(ue=False, rag_calls=0) == "not_applicable"
    assert _verdict(rag_installed=False, rag_calls=0) == "not_applicable"


def test_rag_installed_delegates_and_never_raises(tmp_path, monkeypatch):
    from coordinator_core.hooks import example_retrieval_repo_detect

    monkeypatch.setattr(example_retrieval_repo_detect, "find_example_retrieval_repo_dir", lambda d: "/x")
    assert rag_use.rag_installed(tmp_path) is True
    monkeypatch.setattr(example_retrieval_repo_detect, "find_example_retrieval_repo_dir", lambda d: None)
    assert rag_use.rag_installed(tmp_path) is False

    def boom(d):
        raise OSError("x")

    monkeypatch.setattr(example_retrieval_repo_detect, "find_example_retrieval_repo_dir", boom)
    assert rag_use.rag_installed(tmp_path) is False


def test_constants_pinned():
    assert rag_use.DANGER_CALLS == 0
    assert rag_use.DEBOUNCE_SECONDS == 180
    assert rag_use.PROBE_INTERVAL_SECONDS == 60
    assert rag_use.HALT_RAG_CORPUS_DOWN == "rag_corpus_down"
    assert "Restore example-retrieval-repo" in rag_use.RESTORE_MESSAGE
