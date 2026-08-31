"""A `repos.*` key that names a CONTAINER of repos is not a repo root.

`repos.fleet_root` carries the `repos.` prefix and none of its semantics: its
value is the directory the fleet's checkouts live UNDER, which on a Windows
box is a bare drive root. Enumerating it as a repo root is a correctness bug
with two measured consequences (2026-08-30):

  - `target_is_registered_repo` answered True for EVERY path on that drive,
    so the write-confinement bump's registered-repo leg classified unrelated
    scratch paths as a registered repo.
  - Rule B7's foreign-root leg reads the same enumeration and compiled the
    drive root into a two-character, case-insensitive pattern, reporting the
    tail of the English word "prefi|x:|" in `block_noncanonical_branch_
    creation`'s advisory -- and the deliberate `[drive-letter: ...]`
    placeholder in `guard_concrete_path_citations`' own copy -- as absolute
    foreign repo roots. Both are the same false positive.

The exclusion itself is not new: `coordinator/bin/lib/git_hook_install.py`
has carried `_CONTAINER_REGISTRY_KEYS` since its heal sweep hit the mirror
image of this (a correct entry reported as a broken repo). This file pins the
two copies together -- the constant is duplicated rather than imported
because `_write_bump_applicability` sits on the PreToolUse hot path and must
not pull `coordinator/bin/lib` into its import graph.

Negative-spec: this does NOT test rule B7 (`coordinator_core/message_register`
owns that), and it does not assert anything about which keys the live registry
happens to hold -- a box with no `repos.fleet_root` set must still pass.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from coordinator_core.bash_guards import _write_bump_applicability as applicability


def _load_git_hook_install():
    """Load the sibling holder by path -- it lives under `coordinator/bin/lib`,
    which is not an importable package from here."""
    root = Path(__file__).resolve().parents[3]
    path = root / "coordinator" / "bin" / "lib" / "git_hook_install.py"
    spec = importlib.util.spec_from_file_location("_ghi_for_container_keys", path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_container_registry_keys_agree_across_holders():
    other = _load_git_hook_install()
    assert applicability._CONTAINER_REGISTRY_KEYS == other._CONTAINER_REGISTRY_KEYS, (
        "the two copies of _CONTAINER_REGISTRY_KEYS have drifted -- a key "
        "excluded from one enumeration and not the other is exactly the "
        "split-brain this pin exists to prevent"
    )


def test_every_container_key_carries_the_repos_prefix():
    """A key outside the `repos.` namespace would be excluded from an
    enumeration that never looked at it -- a no-op row, and a sign the set is
    being used for something other than its stated job."""
    for key in applicability._CONTAINER_REGISTRY_KEYS:
        assert key.startswith(applicability._REGISTRY_REPOS_PREFIX), key


def test_container_key_is_skipped_by_the_repo_root_enumeration(monkeypatch, tmp_path):
    """The behaviour, driven through a fixture registry rather than the live
    one: a container key's value must not appear among the enumerated roots,
    while an ordinary `repos.*` entry in the same file still does."""
    registry = tmp_path / "registry.toml"
    fleet = tmp_path / "fleet"
    repo = fleet / "a-real-repo"
    repo.mkdir(parents=True)
    registry.write_text(
        "[repos]\n"
        'fleet_root = "%s"\n'
        'a_real_repo = "%s"\n' % (fleet.as_posix(), repo.as_posix()),
        encoding="utf-8",
    )
    monkeypatch.setattr(applicability, "registry_dir", lambda: str(tmp_path))

    roots = applicability._all_registered_repo_roots()

    assert repo.as_posix() in roots
    assert fleet.as_posix() not in roots


def test_a_path_under_the_container_is_not_a_registered_repo(monkeypatch, tmp_path):
    """The consequence that made this break-class: with the container
    enumerated, every sibling of every checkout answered True here."""
    registry = tmp_path / "registry.toml"
    fleet = tmp_path / "fleet"
    repo = fleet / "a-real-repo"
    repo.mkdir(parents=True)
    unrelated = fleet / "not-a-repo-at-all"
    unrelated.mkdir(parents=True)
    registry.write_text(
        "[repos]\n"
        'fleet_root = "%s"\n'
        'a_real_repo = "%s"\n' % (fleet.as_posix(), repo.as_posix()),
        encoding="utf-8",
    )
    monkeypatch.setattr(applicability, "registry_dir", lambda: str(tmp_path))

    assert applicability.target_is_registered_repo(str(repo)) is True
    assert applicability.target_is_registered_repo(str(unrelated)) is False
