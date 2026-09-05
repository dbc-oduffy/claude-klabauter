"""test_cross_repo_memo_outbox_root_parity — every outbox verb must resolve the
same root `list` prints from.

THE DEFECT THIS CLOSES. The 2026-09-03 relocation repointed the `memo_*` op
family from `state/memo-outbox/` to `.coordinator-local/memo-outbox/`, but
three sites in this CLI kept the retired root spelled out as a literal:
`discard` removed from it, `compose` existence-checked and printed it, and
`draft`'s indeterminate-reconcile stat'd it. `list` goes through
`memo.list_outbox`, which merges both roots, so it printed drafts the other
verbs then reported as not-found — a failure indistinguishable from a
mistyped topic, which is why it survived
(`cross-repo/inbox/2026-09-05-example-market-data-repo-em-memo-outbox-discard-is-
the-one-unmigrated-verb.md`).

The invariant is not "discard works" — it is that no verb carries its own
spelling of an outbox root. `memo_draft` owns both roots and the removal
trigger that ends the dual-root window; a second copy of either literal here
goes stale silently the next time that window closes.
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


def _new_root(repo: pathlib.Path) -> pathlib.Path:
    return repo / ".coordinator-local" / "memo-outbox"


def _legacy_root(repo: pathlib.Path) -> pathlib.Path:
    return repo / "state" / "memo-outbox"


def test_draft_at_canonical_root_resolves_there(tmp_path):
    mod = _load_cli_module()
    target = _new_root(tmp_path) / "a-topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\n", encoding="utf-8")

    assert pathlib.Path(
        mod._resolve_outbox_draft_path(str(tmp_path), "a-topic")
    ) == target


def test_draft_at_retired_root_still_resolves(tmp_path):
    """The migration window's whole purpose — drafts staged before the repoint
    are reachable, or the fallback is decorative."""
    mod = _load_cli_module()
    target = _legacy_root(tmp_path) / "b-topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\n", encoding="utf-8")

    assert pathlib.Path(
        mod._resolve_outbox_draft_path(str(tmp_path), "b-topic")
    ) == target


def test_canonical_root_wins_when_a_topic_sits_at_both(tmp_path):
    mod = _load_cli_module()
    for root in (_new_root(tmp_path), _legacy_root(tmp_path)):
        root.mkdir(parents=True)
        (root / "c-topic.md").write_text("---\n", encoding="utf-8")

    assert pathlib.Path(
        mod._resolve_outbox_draft_path(str(tmp_path), "c-topic")
    ) == _new_root(tmp_path) / "c-topic.md"


def test_absent_topic_falls_back_to_the_canonical_root(tmp_path):
    """The create case: `draft`'s reconcile key and `compose`'s not-found
    message both name a path for a file that does not exist yet, and naming
    the retired one there is how a caller is sent to look in the wrong place."""
    mod = _load_cli_module()

    assert pathlib.Path(
        mod._resolve_outbox_draft_path(str(tmp_path), "d-topic")
    ) == _new_root(tmp_path) / "d-topic.md"


def test_write_path_never_resolves_to_the_retired_root(tmp_path):
    """`memo.draft` O_EXCL-creates at the canonical root unconditionally. A
    reconcile that followed the READ resolution would stat a legacy file the
    op never touched and report the write as pre-existing."""
    mod = _load_cli_module()
    legacy = _legacy_root(tmp_path) / "e-topic.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\n", encoding="utf-8")

    assert pathlib.Path(
        mod._outbox_write_path(str(tmp_path), "e-topic")
    ) == _new_root(tmp_path) / "e-topic.md"


def test_no_verb_carries_its_own_spelling_of_an_outbox_root():
    """The literal `os.path.join(..., "state", "memo-outbox", ...)` is what
    made four of five verbs agree and one disagree. Path construction belongs
    to `memo_draft`; the one surviving mention here is the tracked-vs-ignored
    commit hint, which is about git, not about resolution."""
    source = (_BIN_DIR / "cross-repo-memo.py").read_text(encoding="utf-8")

    joins = [
        line
        for line in source.splitlines()
        if '"state", "memo-outbox"' in line
    ]
    assert len(joins) == 1, joins
    assert "legacy_root" in joins[0]
