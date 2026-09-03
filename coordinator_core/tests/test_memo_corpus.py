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

    memo_corpus.memo_corpus_root.cache_clear()
    result = memo_corpus.memo_corpus_root(repo_root)

    assert result == new_root


def test_memo_corpus_root_falls_back_to_legacy_when_only_legacy_present(tmp_path):
    repo_root = str(tmp_path)
    legacy_root = os.path.join(repo_root, "cross-repo")
    os.makedirs(legacy_root)

    memo_corpus.memo_corpus_root.cache_clear()
    result = memo_corpus.memo_corpus_root(repo_root)

    assert result == legacy_root


def test_memo_corpus_root_creates_new_root_when_neither_exists(tmp_path):
    repo_root = str(tmp_path)
    new_root = os.path.join(repo_root, "state", "cross-repo")

    memo_corpus.memo_corpus_root.cache_clear()
    result = memo_corpus.memo_corpus_root(repo_root)

    assert result == new_root


def test_memo_corpus_root_memoizes_per_process(tmp_path):
    repo_root = str(tmp_path)
    legacy_root = os.path.join(repo_root, "cross-repo")
    os.makedirs(legacy_root)

    memo_corpus.memo_corpus_root.cache_clear()
    first = memo_corpus.memo_corpus_root(repo_root)

    new_root = os.path.join(repo_root, "state", "cross-repo")
    os.makedirs(new_root)
    second = memo_corpus.memo_corpus_root(repo_root)

    assert first == legacy_root
    assert second == legacy_root, (
        "memo_corpus_root must stay memoized for the life of the process "
        "even after the on-disk state changes underneath it"
    )

    memo_corpus.memo_corpus_root.cache_clear()


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
