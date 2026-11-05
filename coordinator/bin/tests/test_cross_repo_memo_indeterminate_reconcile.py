"""test_cross_repo_memo_indeterminate_reconcile — `draft` must never hand back
"it may or may not have landed".

THE DEFECT THIS CLOSES. `memo.draft` reached through the warm engine can end
in `-32004 WARM_DISPATCH_INDETERMINATE`, and that envelope covers two
opposite outcomes: three probes on 2026-08-31 produced the same error while
ONE had written its draft and two had written nothing
(`state/bug-backlog/2026-08-31-memo-draft-hangs-past-the-40s-door-deadline.
yaml`). The engine cannot narrow it — `warm/client.py` marks `delivered`
after `flush()`, which only proves the bytes left the client process into the
pipe buffer — so the discrimination has to happen at the one layer that knows
the target path: this CLI, which computes `memo_draft.outbox_dir(<sender_root>)
/ <topic>.md` from argv before dispatching.

WHY `existed_before` IS THE LOAD-BEARING ARGUMENT. A bare post-hoc
`exists()` cannot separate "my write landed" from "a draft for this topic was
already there and the op refused on collision" — both leave a file at the same
path. Sampling before dispatch is what makes the LANDED verdict sound, and
`test_pre_existing_draft_is_not_claimed_as_landed` is the case that fails if
someone later simplifies the helper down to a single stat.

Unit-level by construction: the reconcile is a pure function of (path,
existed_before, topic), extracted precisely so its three outcomes can be
driven without a git worktree, a warm server, or a mutating op.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "cross_repo_memo", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_draft_that_appeared_is_reported_as_landed(tmp_path):
    mod = _load_cli_module()
    target = tmp_path / "state" / "memo-outbox" / "some-topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\n", encoding="utf-8")

    code, note = mod.reconcile_indeterminate_draft(
        str(target), False, "some-topic"
    )

    assert code == mod.DRAFT_INDETERMINATE_LANDED
    assert "DID land" in note
    assert "Do NOT re-run" in note


def test_absent_draft_is_reported_as_safe_to_rerun(tmp_path):
    mod = _load_cli_module()
    target = tmp_path / "state" / "memo-outbox" / "some-topic.md"

    code, note = mod.reconcile_indeterminate_draft(
        str(target), False, "some-topic"
    )

    assert code == mod.DRAFT_INDETERMINATE_NO_WRITE
    assert "NO draft was written" in note
    assert "safe" in note


def test_pre_existing_draft_is_not_claimed_as_landed(tmp_path):
    mod = _load_cli_module()
    target = tmp_path / "state" / "memo-outbox" / "some-topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\n", encoding="utf-8")

    code, note = mod.reconcile_indeterminate_draft(
        str(target), True, "some-topic"
    )

    assert code != mod.DRAFT_INDETERMINATE_LANDED
    assert "ALREADY at" in note
    assert "DID land" not in note


def test_the_two_outcomes_do_not_collide_with_the_rejection_exit_codes():
    mod = _load_cli_module()
    codes = {mod.DRAFT_INDETERMINATE_LANDED, mod.DRAFT_INDETERMINATE_NO_WRITE}
    assert len(codes) == 2
    assert codes.isdisjoint({0, 1, 2, 3})


class _Indeterminate(RuntimeError):
    pass


def _send_harness(mod, monkeypatch, tmp_path, route):
    import argparse
    import sys
    import types

    fake = types.SimpleNamespace(
        route_mutation=route, WarmDispatchIndeterminate=_Indeterminate
    )
    monkeypatch.setitem(sys.modules, "cc_invoke", fake)
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "_warn_if_unregistered_sender", lambda: None)
    monkeypatch.setattr(mod, "_fanout_topics", lambda root, topic: [])
    outbox = tmp_path / "outbox" / "t.md"
    monkeypatch.setattr(mod, "_outbox_write_path", lambda root, topic: str(outbox))
    return argparse.Namespace(topic="t"), outbox.parent / "sent" / "t.md"


def test_send_indeterminate_after_receipt_reports_delivered_without_resend(
    tmp_path, monkeypatch, capsys
):
    mod = _load_cli_module()
    calls = []

    def route(op, payload, root, legacy):
        calls.append(op)
        sent.parent.mkdir(parents=True)
        sent.write_text("---\n", encoding="utf-8")
        raise _Indeterminate("worker_died")

    args, sent = _send_harness(mod, monkeypatch, tmp_path, route)
    rc = mod._cmd_send(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 1
    assert "REFUSED" not in out
    assert str(sent) in out


def test_send_indeterminate_before_receipt_resumes_once_and_renders_receipt(
    tmp_path, monkeypatch, capsys
):
    mod = _load_cli_module()
    calls = []

    def route(op, payload, root, legacy):
        calls.append(op)
        if len(calls) == 1:
            raise _Indeterminate("worker_died")
        return {
            "exit_code": 0,
            "acted": [{"id": "/r/cross-repo/inbox/t.md", "sent_receipt": "sent/t.md"}],
        }

    args, _sent = _send_harness(mod, monkeypatch, tmp_path, route)
    rc = mod._cmd_send(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 2
    assert "Receiver-side:" in out
    assert "REFUSED" not in out


def test_send_indeterminate_twice_still_refuses(tmp_path, monkeypatch, capsys):
    mod = _load_cli_module()
    calls = []

    def route(op, payload, root, legacy):
        calls.append(op)
        raise _Indeterminate("worker_died")

    args, _sent = _send_harness(mod, monkeypatch, tmp_path, route)
    rc = mod._cmd_send(args)

    assert rc == 1
    assert len(calls) == 2
    assert "REFUSED: t:" in capsys.readouterr().out


def test_every_outcome_returns_a_note_that_names_the_path(tmp_path):
    mod = _load_cli_module()
    target = tmp_path / "state" / "memo-outbox" / "some-topic.md"

    for existed_before, make_file in ((False, False), (False, True), (True, True)):
        if make_file and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("---\n", encoding="utf-8")
        _code, note = mod.reconcile_indeterminate_draft(
            str(target), existed_before, "some-topic"
        )
        assert str(target) in note
