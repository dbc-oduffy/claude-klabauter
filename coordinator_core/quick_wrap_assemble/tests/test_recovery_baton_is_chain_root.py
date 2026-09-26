from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinator_core.quick_wrap_assemble import _c3_ancestry

_RECOVERY_SHA_PREDECESSOR_BODY = (
    "---\n"
    "title: example recovery baton\n"
    "kind: recovery\n"
    "predecessor: 9d9d77d3df2740662c2a5e3ef74d82e0702eca90\n"
    "forked_from: null\n"
    "---\n\n# Example recovery baton\n"
)

_RECOVERY_NULL_PREDECESSOR_BODY = (
    "---\n"
    "title: example recovery baton\n"
    "kind: recovery\n"
    "predecessor: null\n"
    "forked_from: null\n"
    "---\n\n# Example recovery baton\n"
)

_NON_RECOVERY_SHA_PREDECESSOR_BODY = (
    "---\n"
    "title: example baton\n"
    "kind: session-handoff\n"
    "predecessor: 9d9d77d3df2740662c2a5e3ef74d82e0702eca90\n"
    "forked_from: null\n"
    "---\n\n# Example baton\n"
)


def _write(root: Path, rel: str, content: str) -> Path:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _pickup(*, classification: str = "handoff", basename: str | None, artifact_path: str | None = None) -> dict[str, Any]:
    return {
        "classification": classification,
        "artifact_path": artifact_path,
        "basename": basename,
        "deliverable_id": None,
        "actioned_memos": [],
        "consumed_predecessor": False,
    }


def test_recovery_baton_with_sha_predecessor_is_chain_root(tmp_path: Path):
    """The bug this row fixes: a `kind: recovery` baton's `predecessor` is a
    crashed session's commit SHA by schema convention, not an ancestor baton —
    it must classify as chain-root and route to /quick-wrap, not
    /workstream-complete."""
    _write(
        tmp_path,
        "state/handoffs/2026-09-26-recovery.md",
        _RECOVERY_SHA_PREDECESSOR_BODY,
    )
    pickup = _pickup(
        basename="2026-09-26-recovery.md",
        artifact_path="state/handoffs/2026-09-26-recovery.md",
    )
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is True
    assert "recovery" in reason


def test_recovery_baton_with_null_predecessor_is_chain_root(tmp_path: Path):
    """A recovery baton reconstructing a session that crashed with no prior
    commit (predecessor: null) is chain-root too — the recovery carve-out
    subsumes the ordinary null-predecessor case."""
    _write(
        tmp_path,
        "state/handoffs/2026-09-26-recovery.md",
        _RECOVERY_NULL_PREDECESSOR_BODY,
    )
    pickup = _pickup(
        basename="2026-09-26-recovery.md",
        artifact_path="state/handoffs/2026-09-26-recovery.md",
    )
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is True
    assert "recovery" in reason


def test_non_recovery_baton_with_sha_predecessor_still_fails_condition_3(tmp_path: Path):
    """The carve-out is scoped to `kind: recovery` only — an ordinary baton
    whose predecessor happens to be a SHA-shaped string still carries ancestry
    and still fails c3."""
    _write(
        tmp_path,
        "state/handoffs/2026-09-26-baton.md",
        _NON_RECOVERY_SHA_PREDECESSOR_BODY,
    )
    pickup = _pickup(
        basename="2026-09-26-baton.md",
        artifact_path="state/handoffs/2026-09-26-baton.md",
    )
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is False
    assert "carries ancestry" in reason
