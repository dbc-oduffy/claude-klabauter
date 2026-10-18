"""Parity of the engine's design-registry reader with DoE's reference reader on DoE's shared fixtures.

Source: coordinator-content-repo at 5eba5f50de, `coordinator/tests/fixtures/design-registry/{registry.yaml,malformed.yaml}`
(byte copies in fixtures/design_registry/) and `coordinator/hooks/scripts/_design_registry_gate.py`.
`expected_hits.json` is DoE's own answer, produced by running that file's `load(root)` and
`_intersects(glob, path)` over the fixtures, the path list recorded in the golden, and one inline
registry (BOM, CRLF, `?`, zero-segment `**`, `sources: []`): git archive 5eba5f50de of
coordinator/hooks, coordinator/lib and the fixtures dir into a scratch dir, then a script importing
`_design_registry_gate as drg` that calls `drg.load` on a tmp repo holding each registry text.
Nothing here skips: a missing fixture is a failure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core import design_registry as dr

_FIXTURES = Path(__file__).parent / "fixtures" / "design_registry"
_GOLDEN_SHA256 = "c19b4a96bbea376cd8b08a3b3cae6e806b259228df1be8efef2efe063813214a"
_DOE_SHA = "5eba5f50de"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads((_FIXTURES / "expected_hits.json").read_text(encoding="utf-8"))


def _repo(tmp_path: Path, data: bytes) -> Path:
    reg = tmp_path / dr.DEFAULT_PATH
    reg.parent.mkdir(parents=True)
    reg.write_bytes(data)
    return tmp_path


def _pairs(hits) -> list:
    return [[h["id"], p] for h in hits for p in h["paths"]]


def _pairs_in_golden_order(hits, golden_pairs) -> list:
    got = set(map(tuple, _pairs(hits)))
    return [list(p) for p in map(tuple, golden_pairs) if p in got]


def test_golden_sha_matches_the_docstring_pin():
    assert _sha(_FIXTURES / "expected_hits.json") == _GOLDEN_SHA256


def test_vendored_fixtures_are_the_doe_bytes(golden):
    assert golden["doe_sha"] == _DOE_SHA
    for name, sha in golden["fixture_sha256"].items():
        assert _sha(_FIXTURES / name) == sha, name


def test_load_equals_doe_entries_key_for_key(tmp_path, golden):
    entries, code = dr.load(_repo(tmp_path, (_FIXTURES / "registry.yaml").read_bytes()))
    assert code is None
    assert entries == golden["registry"]["entries"]


def test_lookup_equals_doe_intersection_pairs(tmp_path, golden):
    repo = _repo(tmp_path, (_FIXTURES / "registry.yaml").read_bytes())
    hits, code = dr.lookup(repo, golden["registry"]["paths"])
    assert code is None
    expected = golden["registry"]["pairs"]
    assert len(_pairs(hits)) == len(expected)
    assert _pairs_in_golden_order(hits, expected) == expected
    assert [h["id"] for h in hits] == list(dict.fromkeys(i for i, _ in expected))


def test_golden_covers_every_glob_shape(golden):
    covered = {tuple(p) for p in golden["registry"]["pairs"]}
    paths = golden["registry"]["paths"]
    for hit in (
        ("settings", "src/components/settings/a/b.tsx"),  # nested **
        ("settings", "src/components/settings/a.tsx"),  # zero-segment-adjacent **
        ("auth-sign-in", "src/components/auth/form.tsx"),  # * within a segment
        ("exec-decisions-detail", "src/app/(dashboard)/exec/decisions/[decisionId]/page.tsx"),
        ("settings", "src\\components\\settings\\a.tsx"),  # backslash path
        ("settings", "src/app/settings/"),  # trailing-slash directory
    ):
        assert hit in covered, hit
    for miss in (
        "src/components/other/a.tsx",
        "src/components/auth/deep/form.tsx",
        "src/Components/auth/form.tsx",  # case
        "src/app/(dashboard)/exec/decisions/x/page.tsx",
        "src/app/dashboard/exec/decisions/d/page.tsx",
        "docs/readme.md",
        "lib/",
    ):
        assert miss in paths
        assert not any(p == miss for _, p in covered), miss


def test_extra_registry_with_bom_crlf_question_and_empty_sources(tmp_path, golden):
    extra = golden["extra"]
    repo = _repo(tmp_path, extra["text"].encode("utf-8"))
    entries, code = dr.load(repo)
    assert code is None
    assert entries == extra["entries"]
    hits, code = dr.lookup(repo, extra["paths"])
    assert code is None
    assert _pairs_in_golden_order(hits, extra["pairs"]) == extra["pairs"]
    assert len(_pairs(hits)) == len(extra["pairs"])
    assert "no-sources" not in {h["id"] for h in hits}
    assert any(e["id"] == "no-sources" and e["sources"] == [] for e in entries)


def test_malformed_fixture_is_unparseable_as_in_doe(tmp_path, golden):
    assert golden["malformed"] == {"entries": None, "code": "registry-unparseable"}
    entries, code = dr.load(_repo(tmp_path, (_FIXTURES / "malformed.yaml").read_bytes()))
    assert (entries, code) == (None, "registry-unparseable")
    assert dr.lookup(tmp_path, ["src/b/x.ts"]) == (None, "registry-unparseable")
