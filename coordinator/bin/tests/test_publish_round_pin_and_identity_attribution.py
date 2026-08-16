"""test_publish_round_pin_and_identity_attribution — direct unit coverage
for the SHA-pin mechanism (`_round_pin_source_sha`) and the identity-finding
attribution helpers (`_attribute_identity_finding_row`,
`_attribute_identity_findings`) added by the s5-sha-pin-identity-hoist
slice. These three functions previously shipped with zero direct tests
despite `_round_pin_source_sha` being the mechanism the slice's own commit
message calls out as the more important half.

Run: python -m pytest coordinator/bin/tests/test_publish_round_pin_and_identity_attribution.py -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

# Spawns real git subprocesses; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_round_pin_identity_attribution_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit_all(path: Path, message: str) -> str:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _make_target(name: str, dest_dir: Path, source_dir: Path, *, mode: str = "mirror"):
    return publish.ResolvedTarget(
        name=name,
        mode=mode,
        source_dir=source_dir,
        dest_dir=dest_dir,
    )


# ---------------------------------------------------------------------------
# `_round_pin_source_sha`
# ---------------------------------------------------------------------------
def test_round_pin_source_sha_caches_once_per_toplevel(tmp_path):
    """Two contributing roots (subdirectories) sharing one git toplevel
    must reuse the SAME cached sha — the whole point of the round pin is
    that every row reading a shared toplevel agrees, not just that each
    root individually resolves correctly."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    (repo / "a" / "f.txt").write_text("a", encoding="utf-8")
    (repo / "b" / "f.txt").write_text("b", encoding="utf-8")
    head = _commit_all(repo, "init")

    pinned: "dict[str, str]" = {}
    out = io.StringIO()
    sha_a = publish._round_pin_source_sha(repo / "a", pinned, out=out, late=False)
    sha_b = publish._round_pin_source_sha(repo / "b", pinned, out=out, late=False)

    assert sha_a == head
    assert sha_b == head
    assert sha_a == sha_b
    # Only one cache entry — keyed by toplevel, not by root.
    assert len(pinned) == 1
    # Only the FIRST resolution prints — the second is a cache hit and must
    # not re-announce (round-start pinning would otherwise double-log a
    # shared toplevel once per contributing root).
    assert out.getvalue().count("Round source pinned") == 1


def test_round_pin_source_sha_cache_survives_head_advancing(tmp_path):
    """Once a toplevel is pinned, a later call for the SAME toplevel must
    return the cached sha even if HEAD has since moved — this is the exact
    mechanism that closes the mid-round-commit race."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    head1 = _commit_all(repo, "v1")

    pinned: "dict[str, str]" = {}
    first = publish._round_pin_source_sha(repo, pinned, out=io.StringIO(), late=False)
    assert first == head1

    (repo / "f.txt").write_text("v2", encoding="utf-8")
    _commit_all(repo, "v2")

    second = publish._round_pin_source_sha(repo, pinned, out=io.StringIO(), late=False)
    assert second == head1  # still the round-start pin, not the new HEAD


def test_round_pin_source_sha_late_pin_announces_itself(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    _commit_all(repo, "v1")

    out = io.StringIO()
    publish._round_pin_source_sha(repo, {}, out=out, late=True)
    assert "Round source (pinned late)" in out.getvalue()
    assert "Round source pinned:" not in out.getvalue()


def test_round_pin_source_sha_raises_git_materialize_error_outside_work_tree(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(publish.GitMaterializeError):
        publish._round_pin_source_sha(not_a_repo, {}, out=io.StringIO(), late=True)


def test_round_pin_source_sha_error_degrades_to_per_row_skip_pattern(tmp_path):
    """Mirrors `main`'s round-start pinning loop (publish.py ~8767-8781):
    a `GitMaterializeError` for one root must be caught and reported, never
    left to propagate and abort the whole round — the try/except there is
    what's supposed to guarantee this."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    pinned: "dict[str, str]" = {}
    caught = None
    try:
        publish._round_pin_source_sha(not_a_repo, pinned, out=io.StringIO(), late=False)
    except publish.GitMaterializeError as exc:
        caught = exc
    assert caught is not None
    # The failing root must never be cached as a false pin.
    assert pinned == {}


# ---------------------------------------------------------------------------
# `_delta_row_source_sha` reading through the round pin (Finding 1 fix)
# ---------------------------------------------------------------------------
def test_delta_row_source_sha_reads_through_round_pin_not_fresh_head(tmp_path, monkeypatch):
    """The delta skip-check/record must agree with the sha this row actually
    materialized from — not a fresh HEAD read that a peer's mid-round commit
    could have already moved past the round pin."""
    source_repo = tmp_path / "source"
    _init_repo(source_repo)
    (source_repo / "f.txt").write_text("v1", encoding="utf-8")
    head1 = _commit_all(source_repo, "v1")

    target = _make_target("sample", tmp_path / "dest", source_repo)
    monkeypatch.setattr(publish, "_contributing_roots", lambda t: [source_repo])

    pinned: "dict[str, str]" = {}
    # Simulate round-start pinning having already run before this row.
    publish._round_pin_source_sha(source_repo, pinned, out=io.StringIO(), late=False)

    # A peer session commits mid-round.
    (source_repo / "f.txt").write_text("v2", encoding="utf-8")
    _commit_all(source_repo, "v2")

    sha = publish._delta_row_source_sha(target, pinned)
    assert sha == f"{source_repo}:{head1}"  # the PINNED sha, not the drifted HEAD


# ---------------------------------------------------------------------------
# `_attribute_identity_finding_row` / `_attribute_identity_findings`
# ---------------------------------------------------------------------------
def test_attribute_identity_finding_row_longest_prefix_wins(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "sub").mkdir()
    (repo / "sub" / "nested").mkdir()
    (repo / "root.txt").write_text("x", encoding="utf-8")
    _commit_all(repo, "init")

    toplevel_row = _make_target("toplevel", repo, tmp_path / "src-top")
    sub_row = _make_target("sub", repo / "sub", tmp_path / "src-sub")
    nested_row = _make_target("nested", repo / "sub" / "nested", tmp_path / "src-nested")
    rows = [toplevel_row, sub_row, nested_row]

    resolved = publish._attribute_identity_finding_row("sub/nested/file.txt:1: finding", rows)
    assert resolved is nested_row  # the longest matching prefix, not "sub"


def test_attribute_identity_finding_row_falls_back_to_toplevel(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "sub").mkdir()
    (repo / "root.txt").write_text("x", encoding="utf-8")
    _commit_all(repo, "init")

    toplevel_row = _make_target("toplevel", repo, tmp_path / "src-top")
    sub_row = _make_target("sub", repo / "sub", tmp_path / "src-sub")
    rows = [toplevel_row, sub_row]

    resolved = publish._attribute_identity_finding_row("unrelated/path.txt:1: finding", rows)
    assert resolved is toplevel_row


def test_attribute_identity_finding_row_exact_prefix_match_forward_looking(tmp_path):
    """`rel == prefix` fires only when a finding's path is IDENTICAL to a
    row's whole `dest_subdir` — i.e. `dest_subdir` itself named as a file,
    not a directory. No current store row shape produces this, but the
    branch is forward-looking (a future file-shaped `dest_subdir`), not
    dead code to be deleted — pinning the intended behavior here."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "root.txt").write_text("x", encoding="utf-8")
    _commit_all(repo, "init")

    toplevel_row = _make_target("toplevel", repo, tmp_path / "src-top")
    file_shaped_row = _make_target("file-shaped", repo / "sub.txt", tmp_path / "src-file")
    rows = [toplevel_row, file_shaped_row]

    resolved = publish._attribute_identity_finding_row("sub.txt:1: finding", rows)
    assert resolved is file_shaped_row


def test_attribute_identity_finding_row_none_when_no_rows():
    assert publish._attribute_identity_finding_row("path.txt:1: finding", []) is None


def test_attribute_identity_findings_marks_published_vs_skipped(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "sub").mkdir()
    (repo / "root.txt").write_text("x", encoding="utf-8")
    _commit_all(repo, "init")

    toplevel_row = _make_target("toplevel", repo, tmp_path / "src-top")
    sub_row = _make_target("sub", repo / "sub", tmp_path / "src-sub")
    rows = [toplevel_row, sub_row]

    findings = "sub/file.txt:1: fleet codename 'DoE'\nother.txt:2: another finding"
    annotated = publish._attribute_identity_findings(findings, rows, ["sub"])

    lines = annotated.splitlines()
    assert "[row: sub, skipped this run (pre-existing)]" in lines[0]
    assert "[row: toplevel, published this run]" in lines[1]


def test_attribute_identity_findings_passthrough_when_no_rows():
    findings = "some finding line"
    assert publish._attribute_identity_findings(findings, [], []) == findings
