from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from coordinator_core.session.machinery_paths import SHARE_RELDIR

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

_BIN_DIR = Path(__file__).parent.parent
_SESSION_ENV_VARS = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def _load_module():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("coordinator_doc_new", str(_BIN_DIR / "coordinator-doc-new.py"))
    spec = importlib.util.spec_from_loader("coordinator_doc_new", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def doc_new():
    return _load_module()


@pytest.fixture()
def no_session_env(monkeypatch):
    for var in _SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_dispatched_reader_gets_the_session_scoped_root(doc_new, monkeypatch, no_session_env):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "2f364457-4c88-a41f")

    msg = doc_new._missing_out_message("subagent-sidecar")

    assert f"{SHARE_RELDIR}/2f364457-4c88-a41f/" in msg
    assert "<session-id>" not in msg
    assert "--type subagent-sidecar" in msg


def test_session_id_segment_is_sanitized(doc_new, monkeypatch, no_session_env):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "../../etc/passwd")

    msg = doc_new._missing_out_message("subagent-sidecar")

    leaf = msg.split(f"{SHARE_RELDIR}/", 1)[1].split("/", 1)[0]
    assert "/" not in leaf and "\\" not in leaf
    assert leaf == "....etcpasswd"


def test_identityless_reader_gets_the_missing_identity_named(doc_new, no_session_env):
    msg = doc_new._missing_out_message("subagent-sidecar")

    for var in _SESSION_ENV_VARS:
        assert var in msg
    assert "dispatch brief" in msg
    assert f"{SHARE_RELDIR}/em-unknown" not in msg


def test_no_arm_tells_the_reader_to_just_pass_out(doc_new, monkeypatch, no_session_env):
    identityless = doc_new._missing_out_message("run-report")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-1")
    dispatched = doc_new._missing_out_message("run-report")

    for msg in (identityless, dispatched):
        assert "Pass --out explicitly." not in msg


@pytest.mark.parametrize("doc_type", ["run-report", "subagent-sidecar"])
def test_both_sidecar_types_still_refuse_without_out(doc_type, doc_new, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coordinator-doc-new.py",
            "--type",
            doc_type,
            "--plan",
            "docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md",
            "--chunk",
            "c1",
        ],
    )

    try:
        code = doc_new.main()
    except SystemExit as exc:
        code = exc.code

    assert code == 1
    stderr = capsys.readouterr().err
    assert "--out <path> is required" in stderr
    assert "coordinator_core.subagent_sandbox.provision_report" in stderr
