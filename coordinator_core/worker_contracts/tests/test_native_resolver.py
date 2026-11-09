from pathlib import Path

import pytest

from coordinator_core.codex_runner import resolve


def test_windows_npm_shim_resolves_native_without_shell(tmp_path, monkeypatch):
    shim = tmp_path / "codex.CMD"
    shim.write_text("must never execute")
    native = tmp_path / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"fixture")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    monkeypatch.setattr(resolve.shutil, "which", lambda *a, **kw: str(shim))
    result = resolve.resolve_codex("win32", {"FNM_DIR": str(tmp_path / "absent"), "PATH": "fixture"})
    assert result == [str(native)]
    native.unlink()
    with pytest.raises(resolve.CodexUnavailable, match="native Codex binary absent"):
        resolve.resolve_codex("win32", {"FNM_DIR": str(tmp_path / "absent"), "PATH": "fixture"})
