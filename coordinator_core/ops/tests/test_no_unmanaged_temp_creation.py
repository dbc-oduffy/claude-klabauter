"""Repo-wide guard: no scoped tracked file creates temp storage outside temp_layout."""

from __future__ import annotations

import coordinator_core.ops.check_unmanaged_temp_creation as m

import pytest

pytestmark = pytest.mark.complete


def test_no_unmanaged_temp_creation_site():
    findings = m.scan()
    report = "\n".join(
        f"{f.path}:{f.line} {f.kind} {f.function}  -> create it via temp_layout.managed_temp_dir / make_managed_temp_dir"
        for f in findings
    )
    assert not findings, f"unmanaged temp creation sites:\n{report}"
