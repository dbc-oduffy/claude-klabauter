"""The memo-send closing route: receipt-ref slugs, receiver-inbox detection, performer-em memo rows."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    _is_memo_send_row,
    memo_close_route,
    memo_receipt_slug,
    memo_row_slug,
    read_spine,
)

_SENT = ".coordinator-local/memo-outbox/sent"
_INBOX_ROW = (
    "- id: M1\n  title: notify\n  change_kind: doc-edit\n"
    "  surface: coordinator-content-repo/state/cross-repo/inbox/\n  writes: []\n  disposition: open\n"
    "  body: |\n    Tell them.\n"
)


_DRAFT = "coordinator_core.ops.fleet.memo_draft._classify_receiver_for_draft"
_UNKNOWN = {"rejection_class": "unknown_receiver"}
_URL = "https://github.com/dbc-oduffy/example-fleet-context/issues/3"


@pytest.fixture(autouse=True)
def _registered_receivers(monkeypatch):
    """Registry-independent: every receiver resolves unless a test says otherwise."""
    monkeypatch.setattr(_DRAFT, lambda to, dry_run: None)


@pytest.fixture
def unregistered(monkeypatch):
    monkeypatch.setattr(_DRAFT, lambda to, dry_run: dict(_UNKNOWN))


def _memo_row(task_id="C10"):
    return {
        "id": task_id,
        "performer": "em",
        "kind": "memo-send",
        "memo_to": "example-fleet-context-em",
        "surface": "docs/x.md",
        "receipt": f"{_SENT}/topic.md",
    }


def _resolve(tmp_path, **fields):
    from coordinator_core.ops.plan_tasks_mutate import _DELIVERED_ELSEWHERE, _memo_receipt_refusal

    r = {"id": "C10", "disposition": _DELIVERED_ELSEWHERE, **fields}
    return _memo_receipt_refusal([r], {"C10": _memo_row()}, tmp_path)


def _plan(tmp_path, *rows):
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "p.md"
    path.write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n"
        + "".join(rows)
        + "```\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "ref",
    [f"{_SENT}/topic.md", f"{_SENT.replace('/', chr(92))}\\topic.md", "state/memo-outbox/sent/topic.md"],
)
def test_receipt_slug_accepts_canonical_and_legacy(ref):
    assert memo_receipt_slug(ref) == "topic"


@pytest.mark.parametrize(
    "ref",
    [
        ".coordinator-local/memo-outbox/topic.md",
        f"{_SENT}/a/topic.md",
        f"{_SENT}/topic.txt",
        f"{_SENT}/../topic.md",
        f"{_SENT}/../x/topic.md",
        None,
        7,
    ],
)
def test_receipt_slug_rejects_non_receipts(ref):
    assert memo_receipt_slug(ref) is None


def test_slug_precedence_receipt_then_disposition_ref_then_draft():
    base = {"surface": ".coordinator-local/memo-outbox/draft.md"}
    assert memo_row_slug(base) == "draft"
    assert memo_row_slug({**base, "disposition_ref": f"{_SENT}/ref.md"}) == "ref"
    assert memo_row_slug({**base, "disposition_ref": "abc123"}) == "draft"
    assert memo_row_slug({**base, "disposition_ref": f"{_SENT}/ref.md", "receipt": f"{_SENT}/rcpt.md"}) == "rcpt"


@pytest.mark.parametrize(
    "surface",
    [
        "coordinator-content-repo/state/cross-repo/inbox/",
        "state/cross-repo/inbox",
        "..\\coordinator-content-repo\\state\\cross-repo\\inbox\\",
        "/abs/coordinator-content-repo/state/cross-repo/inbox/x.md",
    ],
)
def test_inbox_surface_is_a_memo_send_row(surface):
    assert _is_memo_send_row({"surface": surface})
    assert _is_memo_send_row({"surface": "x.md", "writes_under": [surface]})


def test_near_miss_inbox_segment_is_not_a_memo_row():
    assert not _is_memo_send_row({"surface": "state/cross-repo/inbox-archive/"})


def test_inbox_row_without_slug_is_held_not_raised(tmp_path):
    exclusions: list = []
    assert read_spine(_plan(tmp_path, _INBOX_ROW), exclusions=exclusions) == []
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-send")]
    assert "sent/<slug>.md" in exclusions[0]["detail"]


def test_inbox_row_with_receipt_reports_delivered_once_receipt_exists(tmp_path):
    row = _INBOX_ROW.replace("  writes: []\n", f"  writes: []\n  memo_to: coordinator-content-repo\n  receipt: {_SENT}/note.md\n")
    plan = _plan(tmp_path, row)
    exclusions: list = []
    read_spine(plan, exclusions=exclusions)
    assert exclusions[0]["reason"] == "memo-send"
    assert f"{_SENT}/note.md" in exclusions[0]["detail"]
    (tmp_path / _SENT).mkdir(parents=True)
    (tmp_path / _SENT / "note.md").write_text("sent\n", encoding="utf-8")
    exclusions = []
    read_spine(plan, exclusions=exclusions)
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-delivered")]


def test_performer_em_memo_row_reads_memo_send_not_em_performed(tmp_path):
    row = (
        "- id: M1\n  title: send\n  change_kind: verification\n  performer: em\n  kind: memo-send\n  memo_to: coordinator-content-repo\n"
        f"  surface: docs/x.md\n  writes: []\n  receipt: {_SENT}/topic.md\n  disposition: open\n"
        "  body: |\n    Send.\n"
    )
    exclusions: list = []
    read_spine(_plan(tmp_path, row), exclusions=exclusions)
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-send")]
    assert "em-discharge" not in exclusions[0]["detail"]
    assert "delivered_elsewhere" in exclusions[0]["detail"]


def test_close_route_names_the_receipt_or_the_placeholder():
    assert f"disposition_ref {_SENT}/topic.md" in memo_close_route("C6", "topic")
    assert "id C6" in memo_close_route("C6", "topic")
    assert "sent/<slug>.md" in memo_close_route("C6", None)


def test_unregistered_receiver_closes_with_other_and_url(unregistered, tmp_path):
    assert _resolve(tmp_path, delivered_via="other", disposition_ref=_URL) == {}
    assert _resolve(tmp_path, delivered_via="other", disposition_ref="http://example.com/x") == {}


@pytest.mark.parametrize("ref", ["", "abc123", "ftp://example.com/x", "https://", "https://a b/c", f"{_SENT}/topic.md"])
def test_unregistered_receiver_non_url_ref_still_refuses(unregistered, tmp_path, ref):
    from coordinator_core.locked_write import MutateAbort

    with pytest.raises(MutateAbort) as exc:
        _resolve(tmp_path, delivered_via="other", disposition_ref=ref)
    assert "closes on a sent receipt with delivered_via memo" in str(exc.value)
    assert "receiver is unregistered" in str(exc.value)


def test_unregistered_receiver_url_with_other_via_still_refuses(unregistered, tmp_path):
    from coordinator_core.locked_write import MutateAbort

    with pytest.raises(MutateAbort):
        _resolve(tmp_path, delivered_via="pr", disposition_ref=_URL)


def test_registered_receiver_url_close_still_refuses(tmp_path):
    from coordinator_core.locked_write import MutateAbort

    with pytest.raises(MutateAbort) as exc:
        _resolve(tmp_path, delivered_via="other", disposition_ref=_URL)
    assert "closes on a sent receipt with delivered_via memo" in str(exc.value)
    assert "receiver is unregistered" not in str(exc.value)


@pytest.mark.parametrize(
    "verdict", [None, "example-retrieval-repo-em", {"rejection_class": "registry_error"}, {"rejection_class": "ambiguous_receiver"}]
)
def test_only_unknown_receiver_verdict_counts_as_unregistered(verdict):
    from coordinator_core.ops.dispatch_emit.memo_rows import memo_receiver_unregistered

    assert memo_receiver_unregistered("example-fleet-context-em", lambda to, dry_run: verdict) is False


def test_unknown_receiver_verdict_counts_as_unregistered_via_injected_classifier():
    from coordinator_core.ops.dispatch_emit.memo_rows import memo_receiver_unregistered

    seen: list = []

    def classify(to, dry_run):
        seen.append(to)
        return dict(_UNKNOWN)

    assert memo_receiver_unregistered(" example-fleet-context-em ", classify) is True
    assert seen == ["example-fleet-context-em"]


def test_successful_resolve_of_unregistered_receiver_row_is_silent(monkeypatch, tmp_path, capsys):
    from coordinator_core.ops.fleet import memo_draft

    monkeypatch.undo()
    warm: list = []
    monkeypatch.setattr("coordinator_core.warm.entry_seam.emit_diagnostic", warm.append)
    row = {**_memo_row(), "memo_to": "zz-no-such-receiver-quiet-probe"}
    from coordinator_core.ops.plan_tasks_mutate import _DELIVERED_ELSEWHERE, _memo_receipt_refusal

    r = {"id": "C10", "disposition": _DELIVERED_ELSEWHERE, "delivered_via": "other", "disposition_ref": _URL}
    assert _memo_receipt_refusal([r], {"C10": row}, tmp_path) == {}
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == "" and warm == []

    result = memo_draft._classify_receiver_for_draft("zz-no-such-receiver-quiet-probe", True)
    assert result["rejection_class"] == "unknown_receiver"
    assert "UNKNOWN RECEIVER" in capsys.readouterr().err
    assert warm and "UNKNOWN RECEIVER" in warm[0]


def test_read_spine_does_not_consult_the_receiver_registry(monkeypatch, tmp_path):
    def boom(to, dry_run):
        raise AssertionError("read_spine must not consult the receiver registry")

    monkeypatch.setattr(_DRAFT, boom)
    row = (
        "- id: M1\n  title: send\n  change_kind: verification\n  performer: em\n  kind: memo-send\n"
        f"  memo_to: example-fleet-context-em\n  surface: docs/x.md\n  writes: []\n  receipt: {_SENT}/topic.md\n"
        "  disposition: open\n  body: |\n    Send.\n"
    )
    exclusions: list = []
    read_spine(_plan(tmp_path, row), exclusions=exclusions)
    assert [e["reason"] for e in exclusions] == ["memo-send"]
