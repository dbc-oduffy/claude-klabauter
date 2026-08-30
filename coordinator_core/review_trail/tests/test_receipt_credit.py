"""
coordinator_core.review_trail.tests.test_receipt_credit

Unit tests for the reviewer-sidecar credit source — the second credit source
review coverage reads now that `state/review-trail/*.json` is frozen (DR-372,
DR-374).

What these tests are FOR, stated because a suite that only asserts the
negative would pass against a reader that reads nothing at all. The defect
being repaired is a STUCK NEGATIVE: the frozen store returned "uncovered" for
every recent commit whether or not review happened. So proving the reader
still says "uncovered" on unreviewed work re-proves the direction that was
already safe. Both directions are pinned here, and the positive
(`test_credits_a_commit_its_session_reviewed`) is the one that fails if the
credit source silently reads nothing.

The ordering rule has its own tests for the same reason in reverse: a wrong
"covered" is strictly worse than the stale "uncovered" it replaces, because
"uncovered" is distrusted and "covered" is acted on. Measured over 400 real
commits, crediting on receipt existence alone made 42% of its credits false.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.review_trail.receipt_credit import receipt_credited_shas

#: Deliberately UNMARKED, unlike every sibling in this package. They carry
#: `cadence` because they carry `spawns_process`; this module spawns nothing
#: and the whole file runs in 0.3s. The defect it guards went unnoticed for
#: 486 commits precisely because nothing in the fast tier could see it, so
#: parking its guard behind a cadence gate would reproduce the conditions
#: that hid it.

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SESSION = "11112222-3333-4444-5555-666677778888"
_OTHER_SESSION = "99998888-7777-6666-5555-444433332222"


def _write_sidecar(
    root: Path,
    session_id: str,
    *,
    stamped_at: str,
    agent_type: str = "code-reviewer",
    body: str = "## Findings\n\nOne real finding.\n",
    receipt_session_id: str | None = None,
    key: str = "review_receipt",
    name: str = "coordinatorcode-reviewer.abc123.md",
) -> Path:
    """Write a reviewer sidecar carrying a receipt block, shaped exactly as
    `provision_report._receipt_block` renders it (four single-quoted scalars
    under one key)."""
    share = root / "state" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)
    path = share / name
    path.write_text(
        "---\n"
        f"agent_type: '{agent_type}'\n"
        f"{key}:\n"
        f"  session_id: '{receipt_session_id if receipt_session_id is not None else session_id}'\n"
        "  agent_id: 'a0c4e2ed8b92a39d4'\n"
        f"  agent_type: '{agent_type}'\n"
        f"  stamped_at: '{stamped_at}'\n"
        "---\n"
        "\n" + body,
        encoding="utf-8",
    )
    return path


def test_credits_a_commit_its_session_reviewed(tmp_path: Path) -> None:
    """THE POSITIVE. Without this, "the repoint works" and "the repoint reads
    nothing and the old always-uncovered survives" are indistinguishable."""
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00")
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == {_SHA_A}


def test_does_not_credit_a_session_with_no_receipt(tmp_path: Path) -> None:
    """THE NEGATIVE. The instrument must still be able to say no."""
    (tmp_path / "state" / "subagent-share" / _SESSION).mkdir(parents=True)
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_does_not_credit_a_commit_authored_after_the_review(tmp_path: Path) -> None:
    """The ordering rule. A reviewer dispatched at T cannot have read a commit
    that did not exist at T. This is the check that keeps 42% of the naive
    join's credits out of the result."""
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-27T16:13:31+00:00")
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:32:24+00:00", _SESSION)]
    )
    assert credited == set()


def test_credits_only_the_commits_that_predate_the_receipt(tmp_path: Path) -> None:
    """One session, one receipt, two commits straddling it — the split is per
    commit, never per session."""
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00")
    credited = receipt_credited_shas(
        tmp_path,
        [
            (_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION),
            (_SHA_B, "2026-08-28T13:00:00+00:00", _SESSION),
        ],
    )
    assert credited == {_SHA_A}


def test_newest_receipt_wins_when_a_session_has_several(tmp_path: Path) -> None:
    """A session reviewed twice: the later review covers commits the earlier
    one predates."""
    _write_sidecar(
        tmp_path, _SESSION, stamped_at="2026-08-28T09:00:00+00:00", name="r1.md"
    )
    _write_sidecar(
        tmp_path, _SESSION, stamped_at="2026-08-28T15:00:00+00:00", name="r2.md"
    )
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T12:00:00+00:00", _SESSION)]
    )
    assert credited == {_SHA_A}


def test_does_not_credit_a_blank_sidecar(tmp_path: Path) -> None:
    """A receipt is stamped at DISPATCH, before the reviewer writes anything,
    so a blank body means the review aborted — not that it passed."""
    _write_sidecar(
        tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00", body="   \n"
    )
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_does_not_credit_a_non_reviewer_agent_type(tmp_path: Path) -> None:
    """`executor` provisions a sidecar too. Only a DELEGATE_REVIEWERS member
    counts as review."""
    _write_sidecar(
        tmp_path,
        _SESSION,
        stamped_at="2026-08-28T12:00:00+00:00",
        agent_type="executor",
    )
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_credits_a_namespaced_agent_type(tmp_path: Path) -> None:
    """Dispatch writes `coordinator:code-reviewer`; the vocabulary holds the
    bare name."""
    _write_sidecar(
        tmp_path,
        _SESSION,
        stamped_at="2026-08-28T12:00:00+00:00",
        agent_type="coordinator:code-reviewer",
    )
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == {_SHA_A}


def test_does_not_credit_an_integrator_receipt(tmp_path: Path) -> None:
    """`integrator_receipt:` records that findings were APPLIED. A review
    whose findings needed no application is still a review, and an
    integrator run without a reviewer receipt is not one."""
    _write_sidecar(
        tmp_path,
        _SESSION,
        stamped_at="2026-08-28T12:00:00+00:00",
        key="integrator_receipt",
    )
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_does_not_credit_across_sessions(tmp_path: Path) -> None:
    """Session B's review does not cover session A's commit."""
    _write_sidecar(tmp_path, _OTHER_SESSION, stamped_at="2026-08-28T12:00:00+00:00")
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_does_not_credit_a_receipt_whose_session_id_mismatches_its_directory(
    tmp_path: Path,
) -> None:
    """A sidecar copied into another session's directory keeps its original
    `session_id`; the directory it sits in is not evidence on its own."""
    _write_sidecar(
        tmp_path,
        _SESSION,
        stamped_at="2026-08-28T12:00:00+00:00",
        receipt_session_id=_OTHER_SESSION,
    )
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_does_not_credit_a_commit_with_no_session_id_trailer(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00")
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", "")]
    )
    assert credited == set()


def test_does_not_credit_an_unparseable_commit_date(tmp_path: Path) -> None:
    """No clock, no comparison, no credit — the conservative direction."""
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00")
    credited = receipt_credited_shas(tmp_path, [(_SHA_A, "not-a-date", _SESSION)])
    assert credited == set()


def test_does_not_credit_an_unparseable_receipt_stamp(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, _SESSION, stamped_at="whenever")
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


@pytest.mark.parametrize("hostile", ["../../etc", "a/b", "..", "with space", "x" * 200])
def test_rejects_a_malformed_session_id_without_touching_the_filesystem(
    tmp_path: Path, hostile: str
) -> None:
    """The trailer is author-controlled commit-message text and is used as a
    directory name, so it is validated before any path join."""
    (tmp_path / "state" / "subagent-share").mkdir(parents=True)
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", hostile)]
    )
    assert credited == set()


def test_returns_empty_when_the_share_root_is_absent(tmp_path: Path) -> None:
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_naive_z_suffixed_stamps_compare_as_utc(tmp_path: Path) -> None:
    """`Z` and a naive stamp both mean UTC here. Reading a naive value as
    local time would shift every comparison by the host's offset."""
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00Z")
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00", _SESSION)]
    )
    assert credited == {_SHA_A}


def test_reads_each_session_directory_once(tmp_path: Path, monkeypatch) -> None:
    """Cost scales with sessions in the range, not commits — the property
    that keeps this inside DR-344's budget on a 1000-commit range."""
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00")

    import coordinator_core.review_trail.receipt_credit as mod

    calls: list[str] = []
    real = mod._counting_receipt_stamps

    def counting(share_dir, session_id):
        calls.append(session_id)
        return real(share_dir, session_id)

    monkeypatch.setattr(mod, "_counting_receipt_stamps", counting)
    mod.receipt_credited_shas(
        tmp_path,
        [
            (_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION),
            (_SHA_B, "2026-08-28T11:30:00+00:00", _SESSION),
        ],
    )
    assert calls == [_SESSION]


def test_never_spawns_a_subprocess(tmp_path: Path, monkeypatch) -> None:
    """The resident store exists because read-time git resolution was
    measured and refused. A shell-out added here reintroduces exactly that
    cost on the gate path."""
    import subprocess

    def explode(*args, **kwargs):
        raise AssertionError("receipt_credit must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00")
    assert receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    ) == {_SHA_A}


def test_does_not_credit_a_kira_receipt(tmp_path: Path) -> None:
    """C3 anti-scope, pinned as an executable assertion: `overengineering-
    reviewer` (Kira) sits in `CLOSE_RECEIPT_REVIEWERS` (the close-floor
    question) but deliberately NOT in `DELEGATE_REVIEWERS` (the commit-credit
    question) -- see `reviewer_vocabulary`'s module docstring, "No second,
    diverging reviewer set". This is the test that would catch a future
    "simplification" that merges the two sets: crediting Kira's receipt here
    would silently arm commit credit the plan never authorised for her."""
    _write_sidecar(
        tmp_path,
        _SESSION,
        stamped_at="2026-08-28T12:00:00+00:00",
        agent_type="overengineering-reviewer",
    )
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", _SESSION)]
    )
    assert credited == set()


def test_multiple_session_id_trailers_are_rejected_not_split(tmp_path: Path) -> None:
    """`git log --format=%(trailers:separator=%x20)` space-joins the values of
    TWO `Session-Id:` trailers on one commit into a single field. That joined
    string contains a space, which `_SESSION_ID_RE` has no branch for, so it
    is rejected outright rather than credited under either half. Un-pinned
    before this test: the mechanism was confirmed by reading the regex, not
    exercised — a future loosening of `_SESSION_ID_RE` (e.g. to permit
    whitespace, or a bug that only strips one side) could silently start
    crediting on a malformed multi-trailer commit with no test to catch it."""
    _write_sidecar(tmp_path, _SESSION, stamped_at="2026-08-28T12:00:00+00:00")
    joined = f"{_SESSION} {_OTHER_SESSION}"
    credited = receipt_credited_shas(
        tmp_path, [(_SHA_A, "2026-08-28T11:00:00+00:00", joined)]
    )
    assert credited == set()
