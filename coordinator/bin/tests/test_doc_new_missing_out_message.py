"""test_doc_new_missing_out_message.py — pytest coverage for coordinator-doc-new's
``--out``-is-required refusal on the two session-scoped sidecar types.

Incident: a workflow-dispatched executor asked to write a subagent-sidecar read
the old refusal ("Pass --out explicitly") as a usage error and had no way to act
on it — the sidecar lives under ``state/subagent-share/<session-id>/`` and the
message named neither the session id it carried nor the identity it would need,
so the executor reported the scaffold as unavailable
(state/improvement-queue/2026-08-21-a-dispatched-executor-cannot-scaffold-it-
7c2ccafdf81a.yaml).

Fix under test: ``_missing_out_message`` splits the refusal by what the *reader*
can reach (docs/wiki/guard-messaging.md § Key Patterns) — a caller with a
resolvable session id gets the session-scoped root resolved for it; a caller
without one gets the missing identity named, including the env vars that carry
it. Neither arm derives an ``--out`` value: the DEC-3 no-default rule (the live
path is computed by ``coordinator_core.dispatch.provision`` at spawn time) is
unchanged, and the exit-1 refusal still fires for both sidecar types.

Coverage:
  test_dispatched_reader_gets_the_session_scoped_root — session id in env is
    resolved into the message, not restated as a formula.
  test_session_id_segment_is_sanitized — a session id carrying separators cannot
    smuggle a directory into the named path.
  test_identityless_reader_gets_the_missing_identity_named — no session id in
    env names all three env vars and offers the dispatch brief instead.
  test_no_arm_tells_the_reader_to_just_pass_out — the pre-fix dead-end phrasing
    is absent from both arms.
  test_both_sidecar_types_still_refuse_without_out — run-report and
    subagent-sidecar both exit 1 through the shared message.

Runs bash-free and spawn-free -- the end-to-end cases drive main() in-process:
`python -m pytest coordinator/bin/tests/test_doc_new_missing_out_message.py -q`
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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

    assert "state/subagent-share/2f364457-4c88-a41f/" in msg
    assert "<session-id>" not in msg
    assert "--type subagent-sidecar" in msg


def test_session_id_segment_is_sanitized(doc_new, monkeypatch, no_session_env):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "../../etc/passwd")

    msg = doc_new._missing_out_message("subagent-sidecar")

    # The whitelist keeps '.' (it rejects only the degenerate ''/'.'/'..' whole-
    # segment results); what must not survive is a separator that turns the named
    # path into a traversal.
    leaf = msg.split("state/subagent-share/", 1)[1].split("/", 1)[0]
    assert "/" not in leaf and "\\" not in leaf
    assert leaf == "....etcpasswd"


def test_identityless_reader_gets_the_missing_identity_named(doc_new, no_session_env):
    msg = doc_new._missing_out_message("subagent-sidecar")

    for var in _SESSION_ENV_VARS:
        assert var in msg
    assert "dispatch brief" in msg
    assert "state/subagent-share/em-unknown" not in msg


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

    with pytest.raises(SystemExit) as exc:
        doc_new.main()

    assert exc.value.code == 1
    stderr = capsys.readouterr().err
    assert "--out <path> is required" in stderr
    assert "coordinator_core.dispatch.provision" in stderr
