"""Root resolution for `coordinator_core.ops.extract_cited_sidecars`.

WHY A SECOND FILE beside `test_extract_cited_sidecars.py` rather than more
cases in it: that module carries a module-level `pytest.mark.cadence` because
every test in it spawns a real `git ls-files`. Root resolution needs no
subprocess, and the regression it guards against is one a fast-tier run must
catch on the turn it lands -- putting these behind the cadence gate is what
left the gap a code-reviewer found on `196fbbc71e` in the first place. The
marker scope is the justification for the file; if those tests ever stop
spawning, fold this back in.

WHAT IT GUARDS. The machinery relocation moved sidecars from
`state/subagent-share/` to `.coordinator-local/subagent-share/`, and this
module answers "does this cited sidecar still exist" over a corpus that spans
the move. Resolving against either root ALONE is silently wrong in a
different direction: new-root-only reports the entire pre-move corpus as
dangling, old-root-only goes blind to everything written since. Neither
failure raises.
"""

from __future__ import annotations

import os

from coordinator_core.ops.extract_cited_sidecars import (
    _on_disk_session_ids,
    _share_roots,
    _sidecar_filenames,
)

_OLD = os.path.join("state", "subagent-share")
_NEW = os.path.join(".coordinator-local", "subagent-share")

_SID_OLD = "aaaaaaaa-1111-2222-3333-444444444444"
_SID_NEW = "bbbbbbbb-1111-2222-3333-444444444444"
_SID_BOTH = "cccccccc-1111-2222-3333-444444444444"


def _touch(root, *parts) -> None:
    full = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write("sidecar\n")


def _seed(root: str) -> None:
    _touch(root, _OLD, _SID_OLD, "only-old.md")
    _touch(root, _NEW, _SID_NEW, "only-new.md")
    _touch(root, _OLD, _SID_BOTH, "straddler-old.md")
    _touch(root, _NEW, _SID_BOTH, "straddler-new.md")


class TestShareRoots:
    def test_names_both_roots_current_convention_first(self, tmp_path):
        roots = _share_roots(str(tmp_path))

        assert len(roots) == 2
        assert roots[0].endswith(_NEW), "machinery root must be consulted first"
        assert roots[1].endswith(_OLD)


class TestOnDiskSessionIds:
    def test_finds_sessions_under_either_root(self, tmp_path):
        _seed(str(tmp_path))

        found = _on_disk_session_ids(str(tmp_path))

        assert {_SID_OLD, _SID_NEW, _SID_BOTH} <= found

    def test_a_session_under_both_roots_appears_once(self, tmp_path):
        """A set, not a list: the straddler must not be double-counted.

        This is the assertion a `list` return would fail, and the reason the
        union is built as a set rather than concatenated.
        """
        _seed(str(tmp_path))

        found = _on_disk_session_ids(str(tmp_path))

        assert sorted(found).count(_SID_BOTH) == 1

    def test_missing_root_is_not_an_error(self, tmp_path):
        """Only one root exists on a fresh checkout, and on every repo that
        has finished the relocation. A `listdir` on the absent one must not
        propagate."""
        _touch(str(tmp_path), _NEW, _SID_NEW, "only-new.md")

        assert _on_disk_session_ids(str(tmp_path)) == {_SID_NEW}

    def test_no_root_at_all_yields_empty(self, tmp_path):
        assert _on_disk_session_ids(str(tmp_path)) == set()


class TestSidecarFilenames:
    def test_unions_filenames_across_both_roots(self, tmp_path):
        """The straddling session's files live under different roots; naming
        only one root's would report the other's as absent -- the exact
        never-raises failure this union exists to prevent."""
        _seed(str(tmp_path))

        names = _sidecar_filenames(str(tmp_path), _SID_BOTH)

        assert names == ["straddler-new.md", "straddler-old.md"]

    def test_resolves_a_session_present_under_only_the_legacy_root(self, tmp_path):
        """The pre-move corpus. A new-root-only resolution reports every one
        of these as dangling."""
        _seed(str(tmp_path))

        assert _sidecar_filenames(str(tmp_path), _SID_OLD) == ["only-old.md"]

    def test_resolves_a_session_present_under_only_the_machinery_root(self, tmp_path):
        _seed(str(tmp_path))

        assert _sidecar_filenames(str(tmp_path), _SID_NEW) == ["only-new.md"]

    def test_relative_paths_are_computed_per_root_not_per_repo(self, tmp_path):
        """`os.path.relpath` is taken against each root's own session dir.
        Computing it against a single root would prefix the other root's
        files with `../..`-shaped noise instead of a bare nested name."""
        root = str(tmp_path)
        _touch(root, _OLD, _SID_BOTH, os.path.join("nested", "deep-old.md"))
        _touch(root, _NEW, _SID_BOTH, os.path.join("nested", "deep-new.md"))

        names = _sidecar_filenames(root, _SID_BOTH)

        assert names == ["nested/deep-new.md", "nested/deep-old.md"]

    def test_unknown_session_yields_empty(self, tmp_path):
        _seed(str(tmp_path))

        assert _sidecar_filenames(str(tmp_path), "dddddddd-0000-0000-0000-000000000000") == []
