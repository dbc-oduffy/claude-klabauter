
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
