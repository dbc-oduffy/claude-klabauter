"""An unresolved plugin content root skips contract blocks loudly, not silently."""

from __future__ import annotations

from coordinator_core.subagent_sandbox import provision_report


def test_unresolved_plugin_root_names_the_skip_and_the_key(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(provision_report, "_show_toplevel_no_spawn", lambda cwd: str(tmp_path))
    monkeypatch.setattr(provision_report, "resolve_plugin_root", lambda: None)

    result = provision_report.assemble_contract_block_parts_for_payload(
        {"contract_blocks": ["x"]}, cwd=str(tmp_path), report_sidecar_path=None
    )

    assert result is None
    assert "repos.content_root" in capsys.readouterr().err
