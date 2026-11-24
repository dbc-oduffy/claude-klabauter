"""A checker printing a non-UTF-8 byte still yields its findings (publish log: 0x97 killed the reader)."""
import sys
from pathlib import Path

from coordinator_core.ops import percolate_identity_check as mod


def test_cp1252_byte_in_checker_output_keeps_findings(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / mod._IDENTITY_CHECK_SCRIPT_RELATIVE
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\nsys.stdout.buffer.write(b'finding: a \\x97 b\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "resolve_python_interp", lambda: sys.executable)

    result = mod.run_identity_check(str(tmp_path))

    assert result["exit_code"] == 1
    assert "finding: a" in result["findings"]
