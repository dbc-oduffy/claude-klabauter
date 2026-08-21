"""
Tests for coordinator_core.ops.emit.publish_identity.

The memo's collision pairs are the test oracle and are used verbatim, not
paraphrased: owner="a_"/repo="_b" and owner="a"/repo="__b" both stringify to
"a___b" under the naive `${owner}__${repo}` join, and MUST produce different
doc ids under `publish_doc_id`.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

from coordinator_core.ops.emit.publish_identity import publish_doc_id, repo_slug


def test_memo_collision_pair_produces_distinct_doc_ids():
    a = publish_doc_id("a_", "_b")
    b = publish_doc_id("a", "__b")
    assert a != b


def test_naive_join_would_have_collided():
    assert "a_" + "__" + "_b" == "a" + "__" + "__b"


def test_publish_doc_id_matches_spec_formula():
    owner, repo = "SomeOwner", "SomeRepo"
    expected = hashlib.sha256(f"{owner.lower()}/{repo.lower()}".encode("utf-8")).hexdigest()[:16]
    assert publish_doc_id(owner, repo) == expected


def test_publish_doc_id_is_lowercase():
    doc_id = publish_doc_id("OwnerCase", "RepoCase")
    assert doc_id == doc_id.lower()


def test_publish_doc_id_length_is_16():
    assert len(publish_doc_id("owner", "repo")) == 16


def test_publish_doc_id_case_insensitive_on_owner_and_repo():
    assert publish_doc_id("Owner", "Repo") == publish_doc_id("owner", "repo")
    assert publish_doc_id("OWNER", "REPO") == publish_doc_id("owner", "repo")


def test_publish_doc_id_github_legal_punctuation_case_table():
    cases = [
        ("owner.name", "repo.name"),
        ("owner_name", "repo_name"),
        ("owner-name", "repo-name"),
        ("a.b", "c.d"),
        ("a-b_c", "d.e-f"),
    ]
    ids = {publish_doc_id(owner, repo) for owner, repo in cases}
    assert len(ids) == len(cases)


def test_publish_doc_id_distinguishes_similar_punctuation_boundaries():
    # '.' and '-' variants of the same collision shape must also stay distinct.
    assert publish_doc_id("a.", ".b") != publish_doc_id("a", "..b")
    assert publish_doc_id("a-", "-b") != publish_doc_id("a", "--b")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_publish_doc_id_stable_across_fresh_interpreter():
    owner, repo = "stable-owner", "stable-repo"
    expected = publish_doc_id(owner, repo)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from coordinator_core.ops.emit.publish_identity import publish_doc_id;"
                f"print(publish_doc_id({owner!r}, {repo!r}))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.stdout.strip() == expected


def test_publish_doc_id_does_not_use_builtin_hash(monkeypatch):
    calls = []
    real_hash = hash

    def spying_hash(obj):
        calls.append(obj)
        return real_hash(obj)

    import builtins

    monkeypatch.setattr(builtins, "hash", spying_hash)
    publish_doc_id("owner", "repo")
    assert calls == []


def test_repo_slug_preserves_producer_authoritative_casing():
    assert repo_slug("MyOwner", "MyRepo") == "MyOwner/MyRepo"


def test_repo_slug_is_owner_slash_repo():
    assert repo_slug("owner", "repo") == "owner/repo"
