"""Regression pin for the publish-mode descriptor table (chunk C1,
docs/plans/2026-08-10-repo-cut-the-fourth-mode-and-the-table.md).

Two obligations: (1) the derived accessors must reproduce today's six-site
literal tuples byte-for-byte -- that equality IS the regression net chunks
C2-C6 repoint their consumer sites against. (2) per
state/audits/2026-08-03-percolate-table-shaped-test-blind-spot.md, a
table-vs-table assertion on this exact surface has twice sat green over a
live defect -- so at least one test here must drive a real consumer call
path (the actual `sync_mirror`/`sync_flat_mirror` entry points, bound and
invoked with a descriptor's own declared kwargs) rather than merely compare
the table's contents to another table.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_modes  # noqa: E402
from percolate import publish_sync  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Equality-pin: derived accessors reproduce today's literal tuples exactly
# ---------------------------------------------------------------------------


def test_mirror_like_wire_names_matches_outer_gate_and_argparse_choices():
    assert publish_modes.mirror_like_wire_names() == ("mirror", "flat-mirror")


def test_mirror_entry_points_matches_site_3_literal():
    assert publish_modes.mirror_entry_points() == ("sync_mirror", "sync_flat_mirror")


def test_argparse_mode_choices_matches_site_4_literal():
    assert publish_modes.argparse_mode_choices() == ("mirror", "flat-mirror")


def test_dest_bootstrap_parametrize_wire_names_matches_site_6_literal():
    assert publish_modes.dest_bootstrap_parametrize_wire_names() == ("mirror", "flat-mirror")


def test_mirror_wire_name_constant_matches_site_5_named_row_lookup():
    assert publish_modes.MIRROR_WIRE_NAME == "mirror"


def test_declares_the_four_modes_including_repo_cut():
    assert tuple(d.wire_name for d in publish_modes.PUBLISH_MODES) == (
        "mirror",
        "flat-mirror",
        "manifest",
        "repo-cut",
    )


def test_repo_cut_descriptor_field_values():
    descriptor = publish_modes.descriptor_for("repo-cut")
    assert descriptor is not None
    assert descriptor.wire_name == "repo-cut"
    assert descriptor.is_mirror_like is False
    assert descriptor.entry_point == "sync_repo_cut"
    assert descriptor.accepts_renamed_dir_names is False
    assert descriptor.is_bootstrap_bearing is True


def test_adding_repo_cut_does_not_change_existing_mirror_like_accessors():
    """Regression net for the C7a design tension: `repo-cut` is
    `is_mirror_like=False`, so it must NOT appear in any of the four
    `is_mirror_like`-keyed accessors below -- those tuples remain exactly
    today's ("mirror", "flat-mirror") pins, pending C7b's own outer-gate
    arm and a non-mirror-like-keyed accessor."""
    assert publish_modes.mirror_like_wire_names() == ("mirror", "flat-mirror")
    assert publish_modes.mirror_entry_points() == ("sync_mirror", "sync_flat_mirror")
    assert publish_modes.argparse_mode_choices() == ("mirror", "flat-mirror")
    assert publish_modes.dest_bootstrap_parametrize_wire_names() == (
        "mirror",
        "flat-mirror",
    )
    assert "repo-cut" not in publish_modes.mirror_like_wire_names()


def test_manifest_descriptor_has_no_publish_sync_entry_point():
    descriptor = publish_modes.descriptor_for("manifest")
    assert descriptor is not None
    assert descriptor.entry_point is None
    assert descriptor.is_mirror_like is False


# ---------------------------------------------------------------------------
# 2. Descriptor-advertisement, exercised through the real publish_sync entry
#    points -- not by reading the descriptor field alone.
# ---------------------------------------------------------------------------


def test_mirror_descriptor_advertises_accepts_renamed_dir_names():
    mirror = publish_modes.descriptor_for("mirror")
    flat_mirror = publish_modes.descriptor_for("flat-mirror")
    assert mirror.accepts_renamed_dir_names is True
    assert flat_mirror.accepts_renamed_dir_names is False


def test_mirror_bind_kwargs_bind_against_the_real_sync_mirror_signature():
    """Drives the real `check_publish_sync_contract` bind-check shape
    (`inspect.signature(fn).bind_partial(**kwargs)`) against the actual
    `publish_sync.sync_mirror` symbol using the mirror descriptor's own
    declared contract -- proving the descriptor's `bind_kwargs` (including
    `renamed_dir_names`) actually reaches that entry point's real
    signature, not merely a copy of it."""
    mirror = publish_modes.descriptor_for("mirror")
    fn = getattr(publish_sync, mirror.entry_point)
    inspect.signature(fn).bind_partial(**mirror.bind_kwargs)


def test_flat_mirror_bind_kwargs_reject_renamed_dir_names_on_the_real_signature():
    """`sync_flat_mirror` has no `renamed_dir_names` parameter -- the
    flat-mirror descriptor's own `bind_kwargs` must not claim it does."""
    flat_mirror = publish_modes.descriptor_for("flat-mirror")
    fn = getattr(publish_sync, flat_mirror.entry_point)
    assert "renamed_dir_names" not in flat_mirror.bind_kwargs
    inspect.signature(fn).bind_partial(**flat_mirror.bind_kwargs)
    with __import__("pytest").raises(TypeError):
        inspect.signature(fn).bind_partial(renamed_dir_names=None)


def test_repo_cut_bind_kwargs_bind_against_the_real_sync_repo_cut_signature():
    """Symmetric with `test_mirror_bind_kwargs_bind_against_the_real_sync_mirror_signature`
    and `test_flat_mirror_bind_kwargs_reject_renamed_dir_names_on_the_real_signature`:
    proves `repo-cut`'s declared `bind_kwargs` actually reaches the live
    `publish_sync.sync_repo_cut` signature, not merely a copy of it. Closes the
    asymmetry flagged in the descriptor-table review -- two of four descriptors
    had this "reaches a real call path" proof, repo-cut did not."""
    repo_cut = publish_modes.descriptor_for("repo-cut")
    fn = getattr(publish_sync, repo_cut.entry_point)
    inspect.signature(fn).bind_partial(**repo_cut.bind_kwargs)


def test_mirror_and_flat_mirror_entry_points_dispatch_and_honor_renamed_dir_names(
    tmp_path,
):
    """Real call-path exercise (not table-vs-table): actually invoke
    `sync_mirror`/`sync_flat_mirror` for the mirror and flat-mirror
    descriptors, using each descriptor's own `accepts_renamed_dir_names`
    to decide whether `renamed_dir_names=` is passed -- the same decision
    C2's ledger-read guard and C3's dispatch make off this table."""
    ignore = publish_sync.load_ignore(None)

    for descriptor in (
        publish_modes.descriptor_for("mirror"),
        publish_modes.descriptor_for("flat-mirror"),
    ):
        src_dir = tmp_path / descriptor.wire_name / "src"
        dst_dir = tmp_path / descriptor.wire_name / "dst"
        src_dir.mkdir(parents=True)
        dst_dir.mkdir(parents=True)
        (src_dir / "payload.txt").write_text("hello", encoding="utf-8")

        fn = getattr(publish_sync, descriptor.entry_point)
        call_kwargs: dict[str, object] = {}
        if descriptor.accepts_renamed_dir_names:
            call_kwargs["renamed_dir_names"] = None

        synced, removed = fn(src_dir, dst_dir, ignore, False, **call_kwargs)

        assert synced == 1
        assert removed == 0
        assert (dst_dir / "payload.txt").read_text(encoding="utf-8") == "hello"
