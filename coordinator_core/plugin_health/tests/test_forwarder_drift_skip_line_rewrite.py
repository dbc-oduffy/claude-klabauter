"""Regression for the C3 skip-line rewrite (probe row 23b).

Spec backlink: docs/plans/2026-08-30-the-engine-stops-naming-its-own-repo.md § C3.

`_check_extension_axis`'s `doe_root is None` skip line is purely diagnostic
("nothing to compare") and carries no remedy at all -- unlike `_REMEDY`
(row 23a), which stays untouched because it is already command-shaped and
SUBJECT. Naming `DoE-claude` here told the reader nothing they needed to
understand the check was skipped, so the literal is swapped for a generic
phrase.

Negative-spec: the skip line must never contain the literal `DoE-claude`
again, in any casing this module could plausibly emit.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.plugin_health import forwarder_drift as fd


def test_extension_axis_skip_line_uses_generic_phrasing(tmp_path: Path, monkeypatch):
    settings_bin = tmp_path / "settings-bin"
    settings_bin.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    lines, mismatch = fd._check_extension_axis(None, settings_bin)

    assert mismatch == {}
    assert any(
        "engine sibling repo unresolvable" in line and "[skip]" in line
        for line in lines
    )


def test_extension_axis_skip_line_never_names_doe_claude_literally(tmp_path: Path, monkeypatch):
    settings_bin = tmp_path / "settings-bin"
    settings_bin.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    lines, _mismatch = fd._check_extension_axis(None, settings_bin)

    joined = "\n".join(lines)
    assert "DoE-claude" not in joined
    assert "doe-claude" not in joined.lower()
