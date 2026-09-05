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
    # The operator must be steered away from the one action that looks natural
    # after an error and is wrong here.
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
    """The case a single post-hoc stat gets WRONG. A file that was already
    there is not evidence this call wrote anything, and reporting it as LANDED
    would tell the operator their memo is staged when it may not be."""
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
    """1/2/3 are the receiver-rejection classes (`publish_target_rejected`,
    `unknown_receiver` + collision, `registry_error`/`ambiguous_receiver`).
    An indeterminate is not a rejection; collapsing them onto shared codes
    would re-create the ambiguity at the exit-status layer after removing it
    from the message."""
    mod = _load_cli_module()
    codes = {mod.DRAFT_INDETERMINATE_LANDED, mod.DRAFT_INDETERMINATE_NO_WRITE}
    assert len(codes) == 2
    assert codes.isdisjoint({0, 1, 2, 3})


def test_every_outcome_returns_a_note_that_names_the_path(tmp_path):
    """The operator's next move is always 'go look at that file', so no branch
    may report a verdict without saying which path it stat'd."""
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
