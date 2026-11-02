"""Drift pin and vector check for the vendored example-retrieval-repo unknown-revision v1 contract.

The two vendored files are byte copies of example-retrieval-repo ``contracts/unknown-revision/v1``
at PINNED_SHA. The sha256 constants hold the pin with no sibling checkout; when the
checkout is present the blobs at PINNED_SHA are compared too.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.git_native import cat_file_batch
from coordinator_core.rea_ledger.canonical import canonical_bytes, revision_digest

PINNED_SHA = "1fd7164e7c"
SOURCE_PATHS = {
    "schema.json": "contracts/unknown-revision/v1/schema.json",
    "vectors.json": "contracts/unknown-revision/v1/vectors.json",
}
PINNED_SHA256 = {
    "schema.json": "722b6951e53929d670351de27dd3b9b3974d7314b9ba0db37a2abe0187edcb75",
    "vectors.json": "f5bb8475d439cf471526e1a88aabdff5fa092a499266a5f0ec2f3951e2aa5edf",
}
_VENDORED = Path(__file__).resolve().parents[1] / "vendored" / "unknown-revision" / "v1"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _vectors() -> dict:
    return json.loads((_VENDORED / "vectors.json").read_text(encoding="utf-8"))


def _matches_pin(name: str, data: bytes) -> bool:
    return hashlib.sha256(data).hexdigest() == PINNED_SHA256[name]



def _chain_verifies(records: list[dict]) -> bool:
    prior = None
    for index, record in enumerate(records, start=1):
        if record["revision"] != index or record["previous_revision_digest"] != prior:
            return False
        prior = revision_digest(record)
    return True


@pytest.mark.parametrize("name", sorted(PINNED_SHA256))
def test_vendored_bytes_match_pinned_sha256(name: str) -> None:
    assert _matches_pin(name, (_VENDORED / name).read_bytes())


@pytest.mark.parametrize("name", sorted(PINNED_SHA256))
def test_a_single_edited_byte_breaks_the_pin(name: str) -> None:
    data = bytearray((_VENDORED / name).read_bytes())
    data[len(data) // 2] ^= 0x01
    assert not _matches_pin(name, bytes(data))


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_vendored_bytes_match_sibling_blobs_at_pinned_sha() -> None:
    sibling = _REPO_ROOT.parent / "project-rag"
    got = (
        cat_file_batch(sibling, PINNED_SHA, list(SOURCE_PATHS.values()))
        if (sibling / ".git").exists()
        else {}
    )
    blobs = {name: got.get(path) for name, path in SOURCE_PATHS.items()}
    if any(blob is None for blob in blobs.values()):
        pytest.skip(f"example-retrieval-repo checkout or commit {PINNED_SHA} absent; sha256 pin covers the bytes")
    for name, blob in blobs.items():
        assert (_VENDORED / name).read_bytes() == blob.encode("utf-8"), name


def test_valid_vectors_reproduce_canonical_bytes_and_digest() -> None:
    valid = _vectors()["valid"]
    assert len(valid) >= 4
    for vector in valid:
        assert canonical_bytes(vector["record"]).decode("utf-8") == vector["canonical_json"], vector["name"]
        assert revision_digest(vector["record"]) == vector["revision_digest"], vector["name"]


def test_valid_chain_verifies_and_matches_valid_vector_digests() -> None:
    chain = _vectors()["chains"]["valid"]
    assert chain["expect"] == "verifies"
    assert _chain_verifies(chain["records"])
    by_name = {v["name"]: v["revision_digest"] for v in _vectors()["valid"]}
    digests = [revision_digest(r) for r in chain["records"]]
    assert digests == [by_name[n] for n in ("revision-1-non-ascii", "revision-2", "revision-3")]


def test_broken_chain_is_refused() -> None:
    broken = _vectors()["chains"]["broken"]
    assert broken["expect"] == "refused"
    assert not _chain_verifies(broken["records"])


def test_observed_at_spellings_hash_differently() -> None:
    variants = _vectors()["observed_at_variants"]
    digests = []
    for key in ("a", "b"):
        assert revision_digest(variants[key]["record"]) == variants[key]["revision_digest"]
        digests.append(variants[key]["revision_digest"])
    assert digests[0] != digests[1]


def test_non_canonical_import_is_refused() -> None:
    for vector in _vectors()["refusal"]:
        assert vector["expect"] == "refused"
        assert json.loads(vector["import_text"]) == vector["parses_to"]
        assert canonical_bytes(vector["parses_to"]).decode("utf-8") == vector["canonical_json"]
        assert vector["import_text"].encode("utf-8") != canonical_bytes(vector["parses_to"])


def test_canonical_import_bytes_are_accepted() -> None:
    for vector in _vectors()["valid"]:
        text = vector["canonical_json"].encode("utf-8")
        assert canonical_bytes(json.loads(text)) == text
