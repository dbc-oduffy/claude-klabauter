"""Tests for the scratch-hold nag."""

from __future__ import annotations

import os
import time

from coordinator_core.ops.fleet.scratch_hygiene_nag import hold_nag
from coordinator_core.ops.fleet.tests._scratch_hygiene_fixture import (
    REPO_NAME,
    build_two_root_fixture,
)


def _backdate(path, seconds):
    """Plain os.utime: the shared fixture's age() swallows NotImplementedError on Windows and ages nothing."""
    stamp = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(path):
        for n in dirnames + filenames:
            os.utime(os.path.join(dirpath, n), (stamp, stamp))
    os.utime(path, (stamp, stamp))


def test_two_entries_exact_shape(tmp_path, monkeypatch):
    fx = build_two_root_fixture(tmp_path, monkeypatch)
    try:
        _backdate(fx.hold_missing, 400 * 24 * 3600)
        recs = hold_nag(fx.repo_root)
    finally:
        fx.close()
    by_path = {r["path"]: r for r in recs}
    missing = by_path["scratch-hold/held-no-readme"]
    assert list(missing) == ["op", "kind", "repo", "path", "bytes", "age_days", "readme", "finding"]
    assert missing["op"] == "fleet.scratch_hygiene"
    assert missing["kind"] == "hold-nag"
    assert missing["repo"] == REPO_NAME
    assert missing["bytes"] == 6
    assert missing["readme"] is None
    assert missing["finding"] == "missing-readme"
    assert missing["age_days"] > 365
    ok = by_path["scratch-hold/held-with-readme"]
    assert ok["finding"] == "held"
    assert ok["readme"] == "what | plan-x | after merge"
    assert len(recs) == 2


def test_missing_hold_dir_yields_nothing(tmp_path):
    assert hold_nag(tmp_path / "nowhere") == []


def test_bare_file_sibling_readme_and_md(tmp_path):
    hold = tmp_path / "r" / "scratch-hold"
    hold.mkdir(parents=True)
    (hold / "f.bin").write_bytes(b"123")
    (hold / "f.bin.README").write_text("\n\nbare file note\nmore", encoding="utf-8")
    d = hold / "d"
    d.mkdir()
    (d / "README.md").write_text("# dir note\n", encoding="utf-8")
    recs = {r["path"]: r for r in hold_nag(tmp_path / "r")}
    assert set(recs) == {"scratch-hold/f.bin", "scratch-hold/d"}
    assert recs["scratch-hold/f.bin"]["readme"] == "bare file note"
    assert recs["scratch-hold/d"]["readme"] == "# dir note"


def test_read_only_and_links_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 5000)
    hold = tmp_path / "r" / "scratch-hold"
    hold.mkdir(parents=True)
    link = hold / "lnk"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        from coordinator_core.install.junction import create_junction

        create_junction(link, outside)
    before = sorted(p.name for p in hold.iterdir())
    recs = hold_nag(tmp_path / "r")
    assert [r["path"] for r in recs] == ["scratch-hold/lnk"]
    assert recs[0]["bytes"] < 5000
    assert recs[0]["finding"] == "missing-readme"
    assert sorted(p.name for p in hold.iterdir()) == before
    assert (outside / "big.bin").read_bytes() == b"x" * 5000
