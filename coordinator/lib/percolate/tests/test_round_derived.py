from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate.round_derived import (  # noqa: E402
    EXPECTED_MANIFEST_REL,
    blob_sha,
    is_peer_merge_derived,
    manifest_bytes,
    source_head_of,
)


def test_manifest_bytes_matches_json_form_and_drops_self():
    paths = {"b.txt": "2" * 40, "a.txt": "1" * 40, EXPECTED_MANIFEST_REL: "3" * 40}
    expected = (
        json.dumps(
            {"schema": 1, "source_head": "abc", "paths": {"a.txt": "1" * 40, "b.txt": "2" * 40}},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    assert manifest_bytes(paths, "abc") == expected
    assert EXPECTED_MANIFEST_REL not in json.loads(manifest_bytes(paths, "abc"))["paths"]


def test_blob_sha_matches_git_blob_sha(tmp_path):
    assert blob_sha(b"x") == hashlib.sha1(b"blob 1\0x").hexdigest()


def test_is_peer_merge_derived_both_sides():
    assert is_peer_merge_derived("coordinator_core/_engine_stamp")
    assert is_peer_merge_derived("coordinator_core/_engine_published_at")
    assert is_peer_merge_derived(EXPECTED_MANIFEST_REL)
    assert not is_peer_merge_derived("coordinator_core/contract/x.md")


def test_source_head_of_round_trips_and_rejects_junk():
    assert source_head_of(manifest_bytes({}, "deadbeef")) == "deadbeef"
    assert source_head_of(b"not json") is None
    assert source_head_of(b"[1]") is None
