"""Tests for the single named memo-corpus resolver.

Spec backlink: docs/plans/2026-09-03-the-engine-follows-the-memo-channel-home.md § C1

Negative-spec covered here: a test that only asserts the OLD `<repo>/cross-repo`
path is absent from the result would pass vacuously the moment the function
stops returning anything meaningful -- every assertion below is anchored
POSITIVELY on the expected new-convention path, per the brief's own
instruction that a negative-only assertion goes green exactly when it stops
being true.
"""

import os

from coordinator_core import memo_corpus


def test_memo_corpus_root_prefers_new_root_when_present(tmp_path):
    repo_root = str(tmp_path)
    new_root = os.path.join(repo_root, "state", "cross-repo")
    os.makedirs(new_root)
    legacy_root = os.path.join(repo_root, "cross-repo")
    os.makedirs(legacy_root)

    result = memo_corpus.memo_corpus_root(repo_root)

    assert result == new_root


def test_memo_corpus_root_falls_back_to_legacy_when_only_legacy_present(tmp_path):
    repo_root = str(tmp_path)
    legacy_root = os.path.join(repo_root, "cross-repo")
    os.makedirs(legacy_root)

    result = memo_corpus.memo_corpus_root(repo_root)

    assert result == legacy_root


def test_memo_corpus_root_creates_new_root_when_neither_exists(tmp_path):
    repo_root = str(tmp_path)
    new_root = os.path.join(repo_root, "state", "cross-repo")

    result = memo_corpus.memo_corpus_root(repo_root)

    assert result == new_root


def test_memo_corpus_root_re_resolves_when_this_repo_migrates_mid_process(tmp_path):
    """The inverse of the test this replaces.

    `memo_corpus_root` was process-lifetime `lru_cache`d when it landed, and
    this test pinned that staleness AS CONTRACT -- it asserted the resolver
    must KEEP returning the legacy root after the new one appeared on disk.
    The cache was removed (coordinator:overengineering-reviewer, 2026-09-03):
    it defended against a call pattern the module's own BRIGHTLINE paragraph
    already bans, and it froze the root for `group_em/watch.py`, a long-lived
    process that re-resolves per tick by design.

    So the property flips: a migration landing underneath a running process
    must be PICKED UP, not held at the value resolved first.
    """
    repo_root = str(tmp_path)
    legacy_root = os.path.join(repo_root, "cross-repo")
    os.makedirs(legacy_root)

    first = memo_corpus.memo_corpus_root(repo_root)
    assert first == legacy_root

    new_root = os.path.join(repo_root, "state", "cross-repo")
    os.makedirs(new_root)
    second = memo_corpus.memo_corpus_root(repo_root)

    assert second == new_root, (
        "memo_corpus_root must re-probe per invocation: a repo that migrates "
        "underneath a long-lived process must resolve to the new root on the "
        "next call, not stay frozen at the first resolution"
    )


def test_memo_corpus_root_carries_no_cache_attribute(tmp_path):
    """Grep-proof against the cache coming back by accident.

    `functools.lru_cache` attaches `cache_clear`/`cache_info` to the wrapped
    function, so their ABSENCE is the cheapest positive assertion that this
    resolver is still a plain function.
    """
    assert not hasattr(memo_corpus.memo_corpus_root, "cache_clear")
    assert not hasattr(memo_corpus.memo_corpus_root, "cache_info")


def test_receiver_inbox_root_prefers_new_root_when_present(tmp_path):
    repo_path = str(tmp_path)
    new_root = os.path.join(repo_path, "state", "cross-repo")
    os.makedirs(new_root)

    root, root_isdir = memo_corpus.receiver_inbox_root(repo_path)

    assert root == new_root
    assert root_isdir is True


def test_receiver_inbox_root_falls_back_to_legacy_for_unmigrated_peer(tmp_path):
    repo_path = str(tmp_path)
    legacy_root = os.path.join(repo_path, "cross-repo")
    os.makedirs(legacy_root)

    root, root_isdir = memo_corpus.receiver_inbox_root(repo_path)

    assert root == legacy_root
    assert root_isdir is True


def test_receiver_inbox_root_reports_false_when_neither_exists(tmp_path):
    repo_path = str(tmp_path)
    legacy_root = os.path.join(repo_path, "cross-repo")

    root, root_isdir = memo_corpus.receiver_inbox_root(repo_path)

    assert root == legacy_root
    assert root_isdir is False


def test_receiver_inbox_root_is_not_process_lifetime_cached(tmp_path):
    repo_path = str(tmp_path)
    legacy_root = os.path.join(repo_path, "cross-repo")
    new_root = os.path.join(repo_path, "state", "cross-repo")

    first_root, first_isdir = memo_corpus.receiver_inbox_root(repo_path)
    assert first_root == legacy_root
    assert first_isdir is False

    os.makedirs(new_root)
    second_root, second_isdir = memo_corpus.receiver_inbox_root(repo_path)

    assert second_root == new_root, (
        "receiver_inbox_root must re-probe every call -- a peer migrating "
        "mid-process must be picked up on the very next resolution"
    )
    assert second_isdir is True
