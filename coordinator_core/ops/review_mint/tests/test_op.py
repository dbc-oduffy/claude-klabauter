"""Tests for ``coordinator_core.ops.review_mint.op.load_fragment``, the sibling-clone-touching seam."""

from __future__ import annotations

import pytest

from coordinator_core.ops.review_mint import op as review_mint_op


def test_load_fragment_raises_when_content_root_unresolved(monkeypatch):
    monkeypatch.setattr(review_mint_op, "read_content_root_pointer", lambda: "")
    with pytest.raises(FileNotFoundError):
        review_mint_op.load_fragment()


def test_load_fragment_raises_when_fragment_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(review_mint_op, "read_content_root_pointer", lambda: str(tmp_path))
    with pytest.raises(FileNotFoundError):
        review_mint_op.load_fragment()


def test_load_fragment_reads_and_parses_the_real_relpath(tmp_path, monkeypatch):
    fragment_dir = tmp_path / "coordinator" / "contract"
    fragment_dir.mkdir(parents=True)
    (fragment_dir / "review-roster-fragment.json").write_text(
        '{"schema": "review-roster-fragment", "tiers": {}}', encoding="utf-8"
    )
    monkeypatch.setattr(review_mint_op, "read_content_root_pointer", lambda: str(tmp_path))
    fragment = review_mint_op.load_fragment()
    assert fragment == {"schema": "review-roster-fragment", "tiers": {}}


@pytest.mark.parametrize("layout", ["private", "flat"])
def test_load_fragment_resolves_private_and_flat_mirror(tmp_path, monkeypatch, layout):
    root = tmp_path / "doe"
    content = root / "coordinator" if layout == "private" else root
    (content / "contract").mkdir(parents=True)
    (content / "contract" / "review-roster-fragment.json").write_text('{"schema": "x"}', encoding="utf-8")
    if layout == "flat":
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(review_mint_op, "read_content_root_pointer", lambda: str(root))
    assert review_mint_op.load_fragment() == {"schema": "x"}
