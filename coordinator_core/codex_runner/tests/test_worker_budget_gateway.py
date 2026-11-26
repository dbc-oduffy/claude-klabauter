import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from coordinator_core.codex_runner import workflow
from coordinator_core.codex_runner.workflow import AgentFailed, _budget_gateway_argv, run_agent


def test_budget_binding_confirms_the_issued_listener_and_owner(tmp_path, monkeypatch):
    class Ready(BaseHTTPRequestHandler):
        def do_HEAD(self):
            assert self.path == "/cap-ready"
            self.send_response(204)
            self.send_header("X-Allocation", "arm-A")
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    monkeypatch.delenv("COORDINATOR_PAIR_BUDGET_ID", raising=False)
    assert _budget_gateway_argv(str(tmp_path), None) == []
    path = tmp_path / "budget-gateway.json"
    with HTTPServer(("127.0.0.1", 0), Ready) as server:
        url = f"http://127.0.0.1:{server.server_port}/v1"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for owner in ["arm-B", "arm-A"]:
                path.write_text(json.dumps({"allocation": owner, "endpoint": url}))
                monkeypatch.setenv("COORDINATOR_PAIR_BUDGET_ID", owner)
                if owner == "arm-B":
                    with pytest.raises(AgentFailed, match="confirm allocation"):
                        _budget_gateway_argv(str(tmp_path), url)
                else:
                    assert "model_providers.local_fixture.supports_websockets=false" in _budget_gateway_argv(str(tmp_path), url)
            with pytest.raises(AgentFailed):
                _budget_gateway_argv(str(tmp_path), None)
            with pytest.raises(AgentFailed, match="differs"):
                _budget_gateway_argv(str(tmp_path), "http://127.0.0.1:1/v1")
        finally:
            server.shutdown()
            thread.join()
    with pytest.raises(AgentFailed, match="unavailable"):
        _budget_gateway_argv(str(tmp_path), url)
    monkeypatch.delenv("COORDINATOR_PAIR_BUDGET_ID")
    with pytest.raises(AgentFailed):
        _budget_gateway_argv(str(tmp_path), url)


def test_capped_worker_is_refused_before_native_spawn(tmp_path, monkeypatch):
    """A capped worker with no issued gateway never reaches the native spawn."""
    spawned = []
    monkeypatch.setenv("COORDINATOR_PAIR_BUDGET_ID", "arm-A")
    monkeypatch.delenv("COORDINATOR_LOOPBACK_FIXTURE_URL", raising=False)
    monkeypatch.delenv("COORDINATOR_PROVIDER_EVIDENCE_DIR", raising=False)
    monkeypatch.setattr(workflow, "preflight_auth", lambda **kwargs: None)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **kwargs: ["codex"])
    monkeypatch.setattr(workflow, "shape_codex_child", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "coordinator_core.warm.sandboxed_spawn.caller_is_sandboxed",
        lambda: False,
    )

    def admit(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("native spawn admitted")

    monkeypatch.setattr(workflow, "_run_admitted", admit)
    with pytest.raises(AgentFailed, match="capped worker requires"):
        run_agent(
            "ping",
            {},
            repo_root=tmp_path,
            artifact_dir=tmp_path / "art",
            backend="codex",
            timeout_s=5,
        )
    assert spawned == []
