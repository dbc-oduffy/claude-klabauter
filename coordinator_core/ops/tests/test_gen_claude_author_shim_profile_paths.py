"""Pins the shim op's default PowerShell targets: pwsh 7 and 5.1 profiles under MyDocuments."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops import gen_claude_author_shim as op

_BOM = b"\xef\xbb\xbf"
_PROFILE = "Microsoft.PowerShell_profile.ps1"


def _which(found):
    return lambda exe: f"/bin/{exe}" if exe in found else None


def test_both_hosts_on_path_yield_pwsh7_then_51_profiles_under_mydocuments(tmp_path):
    docs = str(tmp_path / "Docs")
    with mock.patch.object(op, "_my_documents_dir", return_value=docs), mock.patch(
        "shutil.which", _which({"pwsh", "powershell"})
    ):
        got = op._powershell_profile_paths()
    assert got == [
        os.path.join(docs, "PowerShell", _PROFILE),
        os.path.join(docs, "WindowsPowerShell", _PROFILE),
    ]


@pytest.mark.parametrize(
    "found,folder",
    [({"pwsh"}, "PowerShell"), ({"powershell"}, "WindowsPowerShell")],
)
def test_only_the_host_on_path_gets_a_profile(tmp_path, found, folder):
    docs = str(tmp_path / "Docs")
    with mock.patch.object(op, "_my_documents_dir", return_value=docs), mock.patch(
        "shutil.which", _which(found)
    ):
        assert op._powershell_profile_paths() == [os.path.join(docs, folder, _PROFILE)]


def test_no_host_on_path_refuses_naming_rc(tmp_path):
    with mock.patch.object(op, "_my_documents_dir", return_value=str(tmp_path)), mock.patch(
        "shutil.which", _which(set())
    ):
        with pytest.raises(op._ProfileUnresolved, match="--rc"):
            op._powershell_profile_paths()


def test_default_run_refuses_without_writing_when_mydocuments_unresolved(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    tmpl = tmp_path / "claude-author-shim.ps1.tmpl"
    tmpl.write_bytes(b"# x\n")
    env = {"HOME": str(home), "CLAUDE_HOME": str(home), "COORDINATOR_SHIM_RC": ""}
    with mock.patch.dict(os.environ, env), mock.patch.object(
        op, "_my_documents_dir", return_value=None
    ), mock.patch.object(op, "declare_write"):
        rc = op.main(["--shell", "powershell", "--template", str(tmpl)])
    assert rc == 1
    assert "--rc" in capsys.readouterr().err
    assert not (home / "Documents").exists()
    assert not (home / ".claude" / "shell" / "claude-author-shim.ps1").exists()


def test_default_run_writes_utf8_sig_block_into_each_resolved_profile(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    docs = tmp_path / "Docs"
    tmpl = tmp_path / "claude-author-shim.ps1.tmpl"
    tmpl.write_bytes("# em dash —\n".encode("utf-8"))
    env = {"HOME": str(home), "CLAUDE_HOME": str(home), "COORDINATOR_SHIM_RC": ""}
    with mock.patch.dict(os.environ, env), mock.patch.object(
        op, "_my_documents_dir", return_value=str(docs)
    ), mock.patch("shutil.which", _which({"pwsh", "powershell"})), mock.patch.object(
        op, "declare_write"
    ):
        assert op.main(["--shell", "powershell", "--template", str(tmpl)]) == 0
        assert op.main(["--shell", "powershell", "--template", str(tmpl), "--check-only"]) == 0
    for folder in ("PowerShell", "WindowsPowerShell"):
        data = Path(docs, folder, _PROFILE).read_bytes()
        assert data.startswith(_BOM)
        assert op.SENTINEL_BEGIN in data.decode("utf-8-sig")
    assert not (home / "Documents").exists()
