"""
coordinator_core.ops.tests.test_queue_append_output_root_route_scope — the
``QUEUE_APPEND_OUTPUT_ROOT`` redirect is honoured on the in-process route only.

Purpose: ``QUEUE_APPEND_OUTPUT_ROOT`` is a property of a CALLING process (a
test redirecting its own writes into a tmpdir). Under the warm engine the op
executes inside a long-lived server whose environment was inherited from
whichever session spawned it, so one session's exported redirect becomes a
standing redirect for every OTHER session's writes that server handles, for
as long as it lives. Observed live: bug-backlog rows landing in
``pytest-of-<user>/pytest-*/…/state/bug-backlog/`` twice, from two different
shells, while the CLI printed a normal repo path and exited 0 — a silently
lost write.

``queue_append._output_root_override`` closes it by refusing the env read on
any route other than ``IN_PROCESS``. These tests pin both directions: the
genuine in-process test caller keeps working, and a served handler ignores
the server's inherited copy.

Bug: state/bug-backlog/2026-08-19-published-caller-imports-a-mirror-only-name-from-the-live-tree.yaml
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops import queue_append
from coordinator_core.telemetry import op_latency


@pytest.fixture
def _redirect(monkeypatch, tmp_path):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(tmp_path))
    return str(tmp_path)


def test_in_process_route_honours_the_redirect(_redirect, monkeypatch):
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    assert op_latency.execution_route() == op_latency.IN_PROCESS
    assert queue_append._output_root_override() == _redirect


@pytest.mark.parametrize("route", [op_latency.WARM_SERVER, op_latency.HTTP_SERVER])
def test_served_routes_ignore_the_servers_inherited_redirect(_redirect, monkeypatch, route):
    monkeypatch.setenv(op_latency.ROUTE_ENV, route)
    assert op_latency.execution_route() == route
    assert os.environ[queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV] == _redirect
    assert queue_append._output_root_override() is None


def test_absent_env_is_none_on_every_route(monkeypatch):
    monkeypatch.delenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, raising=False)
    for route in (op_latency.IN_PROCESS, op_latency.WARM_SERVER):
        monkeypatch.setenv(op_latency.ROUTE_ENV, route)
        assert queue_append._output_root_override() is None


def test_empty_env_value_is_not_treated_as_a_redirect(monkeypatch):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, "")
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    assert queue_append._output_root_override() is None


# IN_PROCESS route, `QUEUE_APPEND_OUTPUT_ROOT` can name a temp-dir path a


def test_swept_temp_root_refuses_even_in_process(monkeypatch, tmp_path):
    swept = tmp_path / "already-gone"
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(swept))
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    assert op_latency.execution_route() == op_latency.IN_PROCESS
    assert not swept.exists()
    with pytest.raises(queue_append._StaleIsolationRoot):
        queue_append._output_root_override()


def test_live_temp_root_is_still_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    assert queue_append._output_root_override() == str(tmp_path)


# the memo's failure mode: an in-process caller inherits `QUEUE_APPEND_OUTPUT_ROOT`
# from a DIFFERENT, concurrent session's shell environment while that other
# session's temp root is still live (not swept). `_output_root_override` must
# now refuse the latch instead of honouring it unconditionally.


def test_foreign_session_live_root_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)

    monkeypatch.setattr(queue_append, "resolve_current_session_id", lambda *a, **k: "session-A")
    assert queue_append._output_root_override() == str(tmp_path)

    monkeypatch.setattr(queue_append, "resolve_current_session_id", lambda *a, **k: "session-B")
    with pytest.raises(queue_append._ForeignIsolationRoot):
        queue_append._output_root_override()


def test_same_session_reclaiming_its_own_root_is_still_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    monkeypatch.setattr(queue_append, "resolve_current_session_id", lambda *a, **k: "session-A")

    assert queue_append._output_root_override() == str(tmp_path)
    # a second, later CLI invocation (fresh process, same session) reuses the
    # same override root without tripping the foreign-session refusal.
    assert queue_append._output_root_override() == str(tmp_path)


def test_unresolvable_session_id_leaves_ownership_unverified(monkeypatch, tmp_path):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    monkeypatch.setattr(queue_append, "resolve_current_session_id", lambda *a, **k: None)

    # no marker is written and no refusal happens -- unverifiable, not foreign.
    assert queue_append._output_root_override() == str(tmp_path)
    assert not os.path.exists(
        os.path.join(str(tmp_path), queue_append._SESSION_OWNER_MARKER_NAME)
    )


def test_is_swept_tmp_root_predicate(tmp_path):
    live = tmp_path / "still-here"
    live.mkdir()
    swept = tmp_path / "already-gone"
    assert queue_append._is_swept_tmp_root(str(swept)) is True
    assert queue_append._is_swept_tmp_root(str(live)) is False
    assert queue_append._is_swept_tmp_root("/no/such/repo/state/debt-backlog") is False


# Sibling hazard to this module's subject. `QUEUE_APPEND_OUTPUT_ROOT` sends a
# key this op reads so the PUBLISHED engine resolves "the central repo" to


def test_claude_klabauter_root_refuses_a_root_that_is_the_published_mirror(monkeypatch):
    from coordinator_core.ops import queue_append as qa

    monkeypatch.setattr(qa, "_engine_source_root", lambda: None)
    monkeypatch.setattr(qa, "_machine_local_get", lambda key: "/repos/publish-mirror")
    monkeypatch.setattr(qa, "_is_published_engine_mirror", lambda root: True)
    monkeypatch.setattr(qa, "coordinator_engine_root_env", lambda _name: "")

    with pytest.raises(qa._ClaudeKlabauterUnresolvable) as exc:
        qa._claude_klabauter_root()
    assert "PUBLISHED engine mirror" in str(exc.value)


def test_claude_klabauter_root_returns_a_live_working_tree_unchanged(monkeypatch):
    from coordinator_core.ops import queue_append as qa

    monkeypatch.setattr(qa, "_engine_source_root", lambda: None)
    monkeypatch.setattr(qa, "_machine_local_get", lambda key: "/repos/claude-klabauter")
    monkeypatch.setattr(qa, "_is_published_engine_mirror", lambda root: False)
    monkeypatch.setattr(qa, "coordinator_engine_root_env", lambda _name: "")

    assert qa._claude_klabauter_root() == "/repos/claude-klabauter"


def test_mirror_valued_env_override_falls_through_to_the_registry(monkeypatch):
    from coordinator_core.ops import queue_append as qa
    from coordinator_core.telemetry import op_latency

    monkeypatch.setattr(qa, "coordinator_engine_root_env", lambda _name: "/repos/publish-mirror")
    monkeypatch.setattr(op_latency, "execution_route", lambda: op_latency.IN_PROCESS)
    monkeypatch.setattr(qa, "_engine_source_root", lambda: None)
    monkeypatch.setattr(qa, "_machine_local_get", lambda key: "/repos/claude-klabauter")
    monkeypatch.setattr(qa, "_is_published_engine_mirror", lambda root: root == "/repos/publish-mirror")

    assert qa._claude_klabauter_root() == "/repos/claude-klabauter"


def test_mirror_valued_env_and_registry_is_still_refused(monkeypatch):
    from coordinator_core.ops import queue_append as qa
    from coordinator_core.telemetry import op_latency

    monkeypatch.setattr(qa, "coordinator_engine_root_env", lambda _name: "/repos/publish-mirror")
    monkeypatch.setattr(op_latency, "execution_route", lambda: op_latency.IN_PROCESS)
    monkeypatch.setattr(qa, "_engine_source_root", lambda: None)
    monkeypatch.setattr(qa, "_machine_local_get", lambda key: "/repos/publish-mirror")
    monkeypatch.setattr(qa, "_is_published_engine_mirror", lambda root: True)

    with pytest.raises(qa._ClaudeKlabauterUnresolvable):
        qa._claude_klabauter_root()


def test_transform_proof_key_wins_over_a_mirror_naming_registry(monkeypatch):
    from coordinator_core.ops import queue_append as qa

    monkeypatch.setattr(qa, "coordinator_engine_root_env", lambda _name: "")
    monkeypatch.setattr(qa, "_engine_source_root", lambda: "/repos/claude-klabauter")
    monkeypatch.setattr(qa, "_machine_local_get", lambda key: "/repos/publish-mirror")

    assert qa._claude_klabauter_root() == "/repos/claude-klabauter"


def test_absent_transform_proof_key_falls_through_to_the_repo_named_rung(monkeypatch):
    from coordinator_core.ops import queue_append as qa

    monkeypatch.setattr(qa, "coordinator_engine_root_env", lambda _name: "")
    monkeypatch.setattr(qa, "_engine_source_root", lambda: None)
    monkeypatch.setattr(qa, "_machine_local_get", lambda key: "/repos/claude-klabauter")
    monkeypatch.setattr(qa, "_is_published_engine_mirror", lambda root: False)

    assert qa._claude_klabauter_root() == "/repos/claude-klabauter"
