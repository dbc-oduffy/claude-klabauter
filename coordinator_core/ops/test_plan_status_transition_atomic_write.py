"""Plan writes in plan_status_transition are atomic: a failed write keeps the prior bytes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core import atomic_replace
from coordinator_core.ops import plan_status_transition as pst


def test_failed_replace_leaves_prior_bytes(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    original = b"line one\r\nline two\r\n"
    plan.write_bytes(original)

    def boom(src, dst):
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(atomic_replace, "_replace_with_retry", boom)
    with pytest.raises(OSError):
        pst._write_plan_atomic(str(plan), "replacement\n")

    assert plan.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["plan.md"]


def test_write_preserves_crlf_without_translation(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_bytes(b"old\n")
    pst._write_plan_atomic(str(plan), "a\r\nb\n")
    assert plan.read_bytes() == b"a\r\nb\n"


def test_no_truncating_open_remains():
    src = Path(pst.__file__).read_text(encoding="utf-8")
    assert not re.search(r"""open\([^)]*["']w["']""", src)
