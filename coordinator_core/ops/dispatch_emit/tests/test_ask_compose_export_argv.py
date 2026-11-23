"""The clean-export leg prompt names the whole test argv, never targets alone (WinError 193 on Windows)."""
from coordinator_core.ops.dispatch_emit import ask_compose


def test_export_test_instruction_names_interpreter_and_dash_m_pytest(tmp_path):
    text = ask_compose._test_instruction_js("HEAD", str(tmp_path), "EXPORT-CMD")
    assert "-m pytest <the scoped test targets>" in text
    assert "python3 -m pytest" in text or "python" in text.split("-m pytest <the")[0].split("`")[-1]
