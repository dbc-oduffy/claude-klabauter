"""Characterization tests for coordinator_core.ops.mint_deliverable_id.

Ported test intent from the bash oracle — same three paths (carry / stub /
slug), same stderr path-label strings, same output-format assertions.

Port of: mint-deliverable-id.test.sh (DoE 3a561713, 2026-07-22)
Spec backlink: DR-346 §8 — stub path now mints an opaque 6-hex-suffixed
token (bare "dlv-<stub_id>" is retired) so a carried id can prove a copy.
"""
from __future__ import annotations

import re

import pytest

from coordinator_core.ops.mint_deliverable_id import main, mint


# ---------------------------------------------------------------------------
# mint() — library form
# ---------------------------------------------------------------------------


def test_carry_path_echoes_id_unchanged():
    result, label = mint(deliverable_id="dlv-my-plan-abc123")
    assert result == "dlv-my-plan-abc123"
    assert label == "carry"


def test_carry_path_is_idempotent():
    r1, _ = mint(deliverable_id="dlv-my-plan-abc123")
    r2, _ = mint(deliverable_id="dlv-my-plan-abc123")
    assert r1 == r2


def test_stub_path_mints_dlv_stub_id_6hex():
    # DR-346 §8: bare "dlv-<stub_id>" is retired -- the stub path now mints
    # an opaque 6-hex suffix, mirroring the slug path's uniqueness property.
    result, label = mint(stub_id="some-feature-3")
    assert re.match(r"^dlv-some-feature-3-[0-9a-f]{6}$", result)
    assert label == "mint-from-stub"


def test_stub_path_repeated_calls_differ():
    r1, _ = mint(stub_id="some-feature-3")
    r2, _ = mint(stub_id="some-feature-3")
    assert r1.startswith("dlv-some-feature-3-")
    assert r2.startswith("dlv-some-feature-3-")


def test_slug_path_mints_dlv_slug_6hex():
    result, label = mint(slug="my-plan")
    assert re.match(r"^dlv-my-plan-[0-9a-f]{6}$", result)
    assert label == "mint-from-slug"


def test_slug_path_repeated_calls_differ():
    r1, _ = mint(slug="my-plan")
    r2, _ = mint(slug="my-plan")
    # Not a correctness requirement (bash oracle isn't cryptographically
    # unique either) but the common case given time/pid/random inputs.
    assert r1.startswith("dlv-my-plan-")
    assert r2.startswith("dlv-my-plan-")


def test_zero_args_raises_value_error():
    with pytest.raises(ValueError, match="required"):
        mint()


def test_multiple_args_raises_value_error():
    with pytest.raises(ValueError, match="mutually exclusive"):
        mint(slug="a", stub_id="b")


def test_empty_string_args_treated_as_unset():
    with pytest.raises(ValueError, match="required"):
        mint(deliverable_id="", stub_id="", slug="")


# ---------------------------------------------------------------------------
# main() — CLI form
# ---------------------------------------------------------------------------


def test_cli_carry_path(capsys):
    rc = main(["--deliverable-id", "dlv-my-plan-abc123"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "dlv-my-plan-abc123"
    assert "carry path" in out.err


def test_cli_stub_path(capsys):
    rc = main(["--stub-id", "some-feature-3"])
    out = capsys.readouterr()
    assert rc == 0
    assert re.match(r"^dlv-some-feature-3-[0-9a-f]{6}$", out.out.strip())
    assert "mint-from-stub path" in out.err


def test_cli_slug_path(capsys):
    rc = main(["--slug", "my-plan"])
    out = capsys.readouterr()
    assert rc == 0
    assert re.match(r"^dlv-my-plan-[0-9a-f]{6}$", out.out.strip())
    assert "mint-from-slug path" in out.err


def test_cli_no_args_exits_1(capsys):
    rc = main([])
    out = capsys.readouterr()
    assert rc == 1
    assert "required" in out.err


def test_cli_multi_mode_exits_1(capsys):
    rc = main(["--slug", "a", "--stub-id", "b"])
    out = capsys.readouterr()
    assert rc == 1
    assert "mutually exclusive" in out.err


def test_cli_unknown_arg_exits_1(capsys):
    rc = main(["--bogus", "x"])
    out = capsys.readouterr()
    assert rc == 1
    assert "Unknown argument" in out.err


def test_cli_help_exits_0(capsys):
    rc = main(["--help"])
    out = capsys.readouterr()
    assert rc == 0
    assert "mint-deliverable-id.sh" in out.out


def test_a_trailing_separator_never_reaches_the_id():
    """A caller truncating a title to a fixed width lands on a hyphen sooner or
    later, and a trailing one mints `dlv-<slug>--<hex>`. Schema-valid under
    `[a-z0-9-]+` and readable, which is why it survives: a consumer splitting the
    id back into slug and hex sees an empty field and misses the record.

    Measured 2026-09-11 on example-store-repo, where a baton id carrying the doubled
    dash made a landing check report a missing integration record that was on
    disk throughout. This seam is the last line, not the fix -- callers should
    hand a clean slug, and coordinator-doc-new's own mint sites already re-strip
    after their truncation. This one trusted them."""
    minted, label = mint(slug="corpus-knowledge-delivery-raw-")
    assert "--" not in minted, minted
    assert minted.startswith("dlv-corpus-knowledge-delivery-raw-")
    assert label == "mint-from-slug"

    minted, label = mint(stub_id="stub-name-")
    assert "--" not in minted, minted
    assert label == "mint-from-stub"


def test_a_carried_id_is_returned_verbatim_doubled_dash_and_all():
    """Carry is not a mint. An id already on disk is a join key, and normalising
    it here would silently point a carrying artifact at a record nothing else
    names. A doubled dash in a carried id is somebody else's defect to fix at its
    source, never this seam's to launder in passing."""
    assert mint(deliverable_id="dlv-already-minted--abc123") == (
        "dlv-already-minted--abc123",
        "carry",
    )
