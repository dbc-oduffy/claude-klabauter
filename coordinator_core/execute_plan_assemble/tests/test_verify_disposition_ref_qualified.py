"""`<repo_key>:<sha>` disposition_ref verifies against the NAMED repo's own
history, and every value the vendored plan-tasks schema admits for `coded`
clears close-out's shape gate (never MALFORMED)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.execute_plan_assemble.falsifier_shape import (
    _DISPOSITION_REF_QUALIFIED_RE,
    _DISPOSITION_REF_SHA_RE,
)

_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "frontmatter" / "schemas" / "plan-tasks.schema.json"
)


def _git(args: list[str], root: Path) -> str:
    return coas._run_git(args, root).stdout.strip()


def _repo(root: Path, name: str) -> str:
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "test"], root)
    (root / name).write_text(name)
    _git(["add", name], root)
    _git(["commit", "-q", "-m", name], root)
    return _git(["rev-parse", "HEAD"], root)


@pytest.fixture
def two_repos(tmp_path, monkeypatch):
    here = tmp_path / "here"
    sibling = tmp_path / "sibling"
    here_sha = _repo(here, "a.txt")
    sibling_sha = _repo(sibling, "b.txt")
    monkeypatch.setattr(
        coas, "registry_get", lambda key: str(sibling) if key == "repos.klabauter" else None
    )
    return here, sibling, here_sha, sibling_sha


def test_qualified_ref_verifies_in_named_repo(two_repos):
    here, _, here_sha, sibling_sha = two_repos
    assert coas._verify_disposition_ref(here, f"klabauter:{sibling_sha}") == (sibling_sha, None)
    # The same sha is unknown to this repo -- proves the sibling was consulted.
    assert coas._verify_disposition_ref(here, sibling_sha)[1] == coas.DISPOSITION_REF_UNRESOLVABLE
    assert coas._verify_disposition_ref(here, here_sha) == (here_sha, None)


def test_qualified_ref_must_be_ancestor_of_named_repos_head(two_repos):
    here, sibling, _, _ = two_repos
    _git(["checkout", "-q", "-b", "side"], sibling)
    (sibling / "c.txt").write_text("c")
    _git(["add", "c.txt"], sibling)
    _git(["commit", "-q", "-m", "side"], sibling)
    side_sha = _git(["rev-parse", "HEAD"], sibling)
    _git(["checkout", "-q", "-"], sibling)
    assert (
        coas._verify_disposition_ref(here, f"klabauter:{side_sha}")[1]
        == coas.DISPOSITION_REF_NOT_ANCESTOR
    )


def test_unregistered_or_missing_clone_is_unresolvable(two_repos, tmp_path, monkeypatch):
    here, _, _, sibling_sha = two_repos
    assert (
        coas._verify_disposition_ref(here, f"nokey:{sibling_sha}")[1]
        == coas.DISPOSITION_REF_UNRESOLVABLE
    )
    monkeypatch.setattr(coas, "registry_get", lambda key: str(tmp_path / "gone"))
    assert (
        coas._verify_disposition_ref(here, f"klabauter:{sibling_sha}")[1]
        == coas.DISPOSITION_REF_UNRESOLVABLE
    )


@pytest.mark.parametrize("bad", ["k:abc", "a:abcdef1", "K:abcdef1", "klab:HEAD", "klab:abcdef1:abcdef1"])
def test_malformed_qualified_shapes(two_repos, bad):
    here = two_repos[0]
    assert coas._verify_disposition_ref(here, bad)[1] == coas.DISPOSITION_REF_MALFORMED


def test_schema_admitted_coded_refs_pass_close_out_shape_gate():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    pattern = schema["allOf"][1]["then"]["properties"]["disposition_ref"]["pattern"]
    schema_re = re.compile(pattern)
    for value in ("abcdef1", "0123456789abcdef0123456789abcdef01234567",
                  "ab:abcdef1", "repo_key-2:0123456789abcdef"):
        assert schema_re.match(value)
        assert _DISPOSITION_REF_SHA_RE.match(value) or _DISPOSITION_REF_QUALIFIED_RE.match(value)
