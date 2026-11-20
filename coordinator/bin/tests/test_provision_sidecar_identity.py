import runpy
from pathlib import Path

from coordinator_core.session.core import SESSION_ENV_PRECEDENCE


def test_provision_sidecar_uses_native_codex_identity(monkeypatch):
    for name in SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "01a1f6c5-3b76-7bc2-bbc1-09d27dd534b7")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "inherited-claude")
    resolve = runpy.run_path(str(Path(__file__).parents[1] / "provision-sidecar.py"))["_resolve_session_id"]

    assert resolve(None) == "01a1f6c5-3b76-7bc2-bbc1-09d27dd534b7"
    assert resolve("explicit") == "explicit"
