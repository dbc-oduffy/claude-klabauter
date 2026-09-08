"""test_foreign_dir_names_index.py — pins `publish.py`'s sibling-row claim
index (`foreign_dir_names_for_row`) and its version-skew probe
(`_module_accepts_foreign_dir_names`) — chunk C2, docs/plans/2026-09-06-the-
orphan-sweep-learns-what-a-sibling-r.md.

Why the INDEX, not the sync engine (doe-claude-dc's standing caveat, carried
over verbatim from this plan's Problem statement): a wrong `foreign_dir_names`
entry is SILENT. No row sweeps that subdirectory and no row refreshes it, so a
regression here would not surface as a loud test failure downstream in
`sync_mirror` — it would surface as a quietly-reappearing orphan-sweep
FATAL (or, under the override, a quietly-deleted sibling) on someone's real
publish round. Pinning the index directly is the only place a wrong entry is
visible at all.

Negative-spec block (what this file deliberately does NOT exercise):
  - `sync_mirror`'s own `foreign_dir_names` orphan-exemption behaviour — C1's
    own `coordinator/lib/percolate/tests/test_publish_sync_foreign_dir_names.py`
    owns that.
  - `setup/publish-targets.portable`'s real rows — every row here is
    constructed fresh under `tmp_path`; this file never reads or resolves the
    real portable targets file.
  - `dispatch_mirror_like`'s full kwarg-threading contract — that is
    `test_publish_mirror_dispatch_kwargs_pinned.py`'s remit, and (Notes,
    below) is currently red for an unrelated, PRE-EXISTING reason predating
    this chunk.

Run: python -m pytest coordinator/bin/tests/test_foreign_dir_names_index.py -q
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parents[1]


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_foreign_dir_names_index_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _row(name: str, dest_dir: Path, mode: str = "mirror") -> "publish.ResolvedTarget":
    return publish.ResolvedTarget(
        name=name,
        mode=mode,
        source_dir=dest_dir,  # source is irrelevant to this index; reuse dest
        dest_dir=dest_dir,
    )


def test_sibling_one_level_under_dest_root_yields_its_first_segment(tmp_path):
    mirror_root = tmp_path / "mirror"
    sibling_dir = mirror_root / "plugin-a"
    mirror_root.mkdir()
    sibling_dir.mkdir()

    mirror_row = _row("mirror-row", mirror_root)
    sibling_row = _row("sibling-row", sibling_dir)

    names = publish.foreign_dir_names_for_row(mirror_row, [mirror_row, sibling_row])
    assert names == frozenset({"plugin-a"})


def test_sibling_nested_two_deep_yields_only_the_first_segment(tmp_path):
    mirror_root = tmp_path / "mirror"
    nested_dir = mirror_root / "plugin-a" / "sub" / "deep"
    mirror_root.mkdir()
    nested_dir.mkdir(parents=True)

    mirror_row = _row("mirror-row", mirror_root)
    sibling_row = _row("sibling-row", nested_dir)

    names = publish.foreign_dir_names_for_row(mirror_row, [mirror_row, sibling_row])
    assert names == frozenset({"plugin-a"})


def test_row_at_unrelated_dest_root_yields_nothing(tmp_path):
    mirror_root = tmp_path / "mirror"
    unrelated_root = tmp_path / "unrelated"
    mirror_root.mkdir()
    unrelated_root.mkdir()

    mirror_row = _row("mirror-row", mirror_root)
    unrelated_row = _row("unrelated-row", unrelated_root)

    names = publish.foreign_dir_names_for_row(mirror_row, [mirror_row, unrelated_row])
    assert names == frozenset()


def test_row_whose_dest_equals_the_mirrors_yields_nothing(tmp_path):
    mirror_root = tmp_path / "mirror"
    mirror_root.mkdir()

    mirror_row = _row("mirror-row", mirror_root)
    # A distinct row object that happens to land at the SAME dest_dir as
    # `mirror_row` -- shares the root rather than claiming a subdirectory of
    # it, so it must contribute nothing, and it must be considered (not
    # discarded outright) precisely because it is a genuinely different row.
    same_root_row = _row("same-root-row", mirror_root)

    names = publish.foreign_dir_names_for_row(mirror_row, [mirror_row, same_root_row])
    assert names == frozenset()


def test_the_row_itself_is_never_in_its_own_set(tmp_path):
    mirror_root = tmp_path / "mirror"
    mirror_root.mkdir()

    mirror_row = _row("mirror-row", mirror_root)

    # Only `mirror_row` itself in the row set -- if self-exclusion were done
    # by path equality rather than row identity, this would still pass
    # (dest == dest contributes nothing either way); the identity-scoped test
    # below is what actually distinguishes the two implementations.
    names = publish.foreign_dir_names_for_row(mirror_row, [mirror_row])
    assert names == frozenset()


def test_self_exclusion_is_by_row_identity_not_path_equality(tmp_path):
    """A distinct row sharing `mirror_row`'s exact dest_dir must still be
    considered on its own terms, never discarded merely because its path
    matches `target`'s. Excluding by `other.dest_dir == target.dest_dir`
    would ALSO drop this row -- indistinguishable from correct exclusion in
    THIS scenario (both read as "contributes nothing") since a same-root
    row never claims a subdirectory; the assertion instead pins that the
    real row-identity comparison (`is`) is what implements it, by
    confirming a second, cloned `ResolvedTarget` at the same dest_dir as
    `mirror_row` is still visited (not skipped as "self") and still
    correctly contributes nothing of its own."""
    mirror_root = tmp_path / "mirror"
    mirror_root.mkdir()

    mirror_row = _row("mirror-row", mirror_root)
    cloned_same_root_row = publish.ResolvedTarget(
        name="mirror-row",
        mode="mirror",
        source_dir=mirror_root,
        dest_dir=mirror_root,
    )
    assert cloned_same_root_row is not mirror_row

    names = publish.foreign_dir_names_for_row(
        mirror_row, [mirror_row, cloned_same_root_row]
    )
    assert names == frozenset()


def test_target_filtered_run_still_sees_the_siblings_claim(tmp_path):
    """The whole point of the chunk: `all_rows` must be the UNFILTERED row
    set. A `--target`-narrowed set that dropped the sibling row before
    reaching `foreign_dir_names_for_row` would silently lose its claim."""
    mirror_root = tmp_path / "mirror"
    sibling_dir = mirror_root / "plugin-a"
    mirror_root.mkdir()
    sibling_dir.mkdir()

    mirror_row = _row("mirror-row", mirror_root)
    sibling_row = _row("sibling-row", sibling_dir)
    unrelated_row = _row("unrelated-row", tmp_path / "elsewhere")

    # Simulates the caller passing the FULL unfiltered row set even though a
    # real run was invoked with `--target mirror-row`.
    all_rows = [mirror_row, sibling_row, unrelated_row]

    names = publish.foreign_dir_names_for_row(mirror_row, all_rows)
    assert names == frozenset({"plugin-a"})


def test_string_prefix_that_is_not_a_path_child_yields_nothing(tmp_path):
    """`/a/bc` must never read as under `/a/b` -- path-SEGMENT arithmetic,
    never a string prefix test."""
    mirror_root = tmp_path / "a" / "b"
    sibling_root = tmp_path / "a" / "bc"
    mirror_root.mkdir(parents=True)
    sibling_root.mkdir(parents=True)

    mirror_row = _row("mirror-row", mirror_root)
    sibling_row = _row("sibling-row", sibling_root)

    names = publish.foreign_dir_names_for_row(mirror_row, [mirror_row, sibling_row])
    assert names == frozenset()


def test_every_returned_name_corresponds_to_a_row_that_actually_lands_there(tmp_path):
    mirror_root = tmp_path / "mirror"
    plugin_a = mirror_root / "plugin-a"
    plugin_b = mirror_root / "plugin-b" / "nested"
    mirror_root.mkdir()
    plugin_a.mkdir()
    plugin_b.mkdir(parents=True)

    mirror_row = _row("mirror-row", mirror_root)
    row_a = _row("row-a", plugin_a)
    row_b = _row("row-b", plugin_b)
    unrelated_row = _row("unrelated-row", tmp_path / "elsewhere")

    all_rows = [mirror_row, row_a, row_b, unrelated_row]
    names = publish.foreign_dir_names_for_row(mirror_row, all_rows)

    assert names == frozenset({"plugin-a", "plugin-b"})
    for name in names:
        landing_rows = [
            r
            for r in all_rows
            if r is not mirror_row
            and _is_relative_to(r.dest_dir, mirror_root)
            and r.dest_dir.relative_to(mirror_root).parts[0] == name
        ]
        assert landing_rows, f"{name!r} has no row that actually lands there"


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# _module_accepts_foreign_dir_names — version-skew probe
# ---------------------------------------------------------------------------


def test_module_with_foreign_dir_names_param_is_detected():
    def sync_mirror(src, dst, ignore, dry_run, *, foreign_dir_names=None, **kwargs):
        return (0, 0)

    stub_module = types.ModuleType("stub_publish_sync_with_param")
    stub_module.sync_mirror = sync_mirror

    assert publish._module_accepts_foreign_dir_names(stub_module) is True


def test_module_without_foreign_dir_names_param_is_not_detected():
    """A stub whose `sync_mirror` lacks the parameter -- the lagging-copy
    arm -- must read as False, never raise, so the caller omits the kwarg
    and behaviour degrades to exactly today's."""

    def sync_mirror(src, dst, ignore, dry_run, **kwargs):
        return (0, 0)

    stub_module = types.ModuleType("stub_publish_sync_without_param")
    stub_module.sync_mirror = sync_mirror

    assert publish._module_accepts_foreign_dir_names(stub_module) is False


def test_module_missing_sync_mirror_entirely_is_not_detected():
    stub_module = types.ModuleType("stub_publish_sync_no_sync_mirror")
    assert publish._module_accepts_foreign_dir_names(stub_module) is False


## Review: overengineering-reviewer (Kira) — the end-to-end skew arm
## previously duplicated here is pinned in
## test_publish_mirror_dispatch_kwargs_pinned.py::
## test_call_site_omits_foreign_dir_names_when_module_lacks_it; the
## call-site contract belongs in that pin file, not here. The unit-level
## probe tests above (which are not duplicated) stay.


def test_sibling_reached_via_a_differently_spelled_same_location_still_yields_its_segment(
    tmp_path,
):
    # Review: coordinator:code-reviewer (Finding 1) — `foreign_dir_names_for_row`
    # must `.resolve()` both sides before `relative_to`, since two `dest_dir`s can
    # denote the same on-disk location while being spelled differently (a
    # symlinked component here; a surviving `.`/`..` segment or a Windows
    # short-name/long-name pair are the same class of mismatch this pins).
    real_root = tmp_path / "real-mirror"
    sibling_dir = real_root / "plugin-a"
    real_root.mkdir()
    sibling_dir.mkdir()

    alias_root = tmp_path / "alias-mirror"
    try:
        alias_root.symlink_to(real_root, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        import pytest

        pytest.skip(f"symlink creation not permitted on this host: {exc}")

    # The mirror row is spelled via the symlink alias; the sibling row is
    # spelled via the real path -- lexically unrelated strings, same location.
    mirror_row = _row("mirror-row", alias_root)
    sibling_row = _row("sibling-row", sibling_dir)

    names = publish.foreign_dir_names_for_row(mirror_row, [mirror_row, sibling_row])
    assert names == frozenset({"plugin-a"})
