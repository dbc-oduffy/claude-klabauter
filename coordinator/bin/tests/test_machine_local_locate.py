"""Tests for `machine-local locate <root> | --list` (cross-box storage record).

Loads `_machine_local.py` from its file path and drives `main()` in-process against
a tmp_path registry.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"
_spec = importlib.util.spec_from_file_location("_machine_local_locate_under_test", _IMPL)
_ml = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ml
_spec.loader.exec_module(_ml)


class _R:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _invoke_ml_inprocess(argv: list[str], env: dict[str, str]) -> _R:
    old_env, old_argv = dict(os.environ), sys.argv
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        os.environ.clear()
        os.environ.update(env)
        sys.argv = ["machine-local"] + argv
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = _ml.main()
                if isinstance(result, int):
                    code = result
            except SystemExit as exc:
                c = exc.code
                code = 0 if c is None else (c if isinstance(c, int) else 1)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.argv = old_argv
    return _R(code, out.getvalue(), err.getvalue())


def _run_ml(sandbox: str, argv: list[str]) -> _R:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPO_")}
    env["MACHINE_LOCAL_REGISTRY_DIR"] = sandbox
    return _invoke_ml_inprocess(argv, env)


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _fwd(p: str) -> str:
    return p.replace("\\", "/")


def _setup(tmp_path, record: str, box_id: str | None = "Machine-a", with_key: bool = True):
    """Isolated registry dir plus a record file; returns (sandbox, data_dir)."""
    sandbox = str(tmp_path / "reg")
    data = tmp_path / "data"
    data.mkdir(parents=True)
    rec = tmp_path / "fleet" / "storage-roots.toml"
    rec.parent.mkdir(parents=True)
    rec.write_text(record.replace("<DATA>", _fwd(str(data))), encoding="utf-8")
    local = "schema = 1\n"
    if box_id is not None:
        local += f'[box]\nid = "{box_id}"\n'
    if with_key:
        local += f'[storage_registry]\npath = "{_fwd(str(rec))}"\n'
    _write_file(os.path.join(sandbox, "registry.toml"), "schema = 1\n")
    _write_file(os.path.join(sandbox, "registry.local.toml"), local)
    return sandbox, data


def _row(r):
    return r.stdout.strip().split("\t")


_RECORD = """
[roots.here]
kind = "path"
box = "Machine-a"
path = '<DATA>'
purpose = "on this box"

[roots.here-gone]
kind = "path"
box = "Machine-a"
path = '<DATA>/vanished'
purpose = "missing on this box"

[roots.elsewhere]
kind = "path"
box = "Machine-b"
path = '<DATA>'
purpose = "other box"

[roots.nas-root]
kind = "path"
box = "nas"
purpose = "shared"
[roots.nas-root.mounts]
Machine-a = '<DATA>'
Machine-b = "/Volumes/never-mounted-here"

[roots.nas-nomount]
kind = "path"
box = "nas"
purpose = "shared, no mount for this box"
[roots.nas-nomount.mounts]
Machine-b = "/Volumes/x"

[roots.release]
kind = "url"
url = "https://example.invalid/releases"
purpose = "release assets"

[roots.winroot]
kind = "path"
box = "Machine-a"
path = 'F:\\Likeness\\Active'  # abs-path-ok: Windows drive-path string fixture
purpose = "windows drive path"
"""


def test_same_box_exists_and_missing(tmp_path):
    sandbox, data = _setup(tmp_path, _RECORD)
    f = _row(_run_ml(sandbox, ["locate", "here"]))
    assert f[0] == "here" and f[1] == "Machine-a" and _fwd(f[2]) == _fwd(str(data)) and f[3] == "true"
    f = _row(_run_ml(sandbox, ["locate", "here-gone"]))
    assert f[3] == "false"


def test_other_box_is_false_even_when_path_exists_here(tmp_path):
    sandbox, data = _setup(tmp_path, _RECORD)
    assert data.exists()
    r = _run_ml(sandbox, ["locate", "elsewhere"])
    assert r.returncode == 0
    f = _row(r)
    assert f[1] == "Machine-b" and f[3] == "false"


def test_nas_uses_this_boxes_mount(tmp_path):
    sandbox, data = _setup(tmp_path, _RECORD)
    f = _row(_run_ml(sandbox, ["locate", "nas-root"]))
    assert f[1] == "nas" and _fwd(f[2]) == _fwd(str(data)) and f[3] == "true"
    other, _ = _setup(tmp_path / "o", _RECORD, box_id="Machine-b")
    f = _row(_run_ml(other, ["locate", "nas-root"]))
    assert f[2] == "/Volumes/never-mounted-here" and f[3] == "false"
    f = _row(_run_ml(sandbox, ["locate", "nas-nomount"]))
    assert f[2] == "-" and f[3] == "false"


def test_nas_mount_lookup_ignores_box_id_case(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD, box_id="machine-b")
    f = _row(_run_ml(sandbox, ["locate", "nas-root"]))
    assert f[2] == "/Volumes/never-mounted-here" and f[3] == "false"
    f = _row(_run_ml(sandbox, ["locate", "nas-nomount"]))
    assert f[2] == "/Volumes/x"


def test_url_root_is_unprobed(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD)
    r = _run_ml(sandbox, ["locate", "release"])
    assert r.returncode == 0
    f = _row(r)
    assert f[2] == "https://example.invalid/releases" and f[3] == "unprobed"
    unset, _ = _setup(tmp_path / "u", _RECORD, box_id=None)
    assert _row(_run_ml(unset, ["locate", "release"]))[3] == "unprobed"


def test_windows_drive_path_printed_verbatim(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD)
    r = _run_ml(sandbox, ["locate", "winroot"])
    assert r.returncode == 0
    f = _row(r)
    assert f[2] == "F:\\Likeness\\Active"  # abs-path-ok: Windows drive-path string fixture
    assert f[3] == ("true" if os.path.exists(f[2]) else "false")


def test_runs_from_a_foreign_cwd(tmp_path, monkeypatch):
    sandbox, data = _setup(tmp_path, _RECORD)
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    r = _run_ml(sandbox, ["locate", "here"])
    assert r.returncode == 0 and _row(r)[3] == "true"


def test_unset_box_id_prints_unknown_and_hint(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD, box_id=None)
    r = _run_ml(sandbox, ["locate", "here"])
    assert r.returncode == 0
    assert _row(r)[3] == "unknown"
    assert "machine-local set box.id" in r.stderr


def test_unknown_name_exit_1(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD)
    r = _run_ml(sandbox, ["locate", "no-such-root"])
    assert r.returncode == 1 and r.stdout == ""
    assert "--list" in r.stderr


def test_list_prints_every_root(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD)
    r = _run_ml(sandbox, ["locate", "--list"])
    assert r.returncode == 0
    rows = [ln.split("\t") for ln in r.stdout.splitlines()]
    assert [x[0] for x in rows] == [
        "here", "here-gone", "elsewhere", "nas-root", "nas-nomount", "release", "winroot",
    ]
    assert all(len(x) == 4 for x in rows)


def test_missing_record_exit_2(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD)
    Path(tmp_path / "fleet" / "storage-roots.toml").unlink()
    r = _run_ml(sandbox, ["locate", "here"])
    assert r.returncode == 2 and "locate" in r.stderr
    assert "storage_registry.path" in r.stderr  # the error names its remedy


def test_malformed_record_exit_2(tmp_path):
    sandbox, _ = _setup(tmp_path, "[[[ not toml")
    r = _run_ml(sandbox, ["locate", "here"])
    assert r.returncode == 2 and "malformed toml" in r.stderr.lower()


def test_no_argument_is_operational_failure(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD)
    assert _run_ml(sandbox, ["locate"]).returncode == 2


def test_content_root_fallback_when_key_unset(tmp_path):
    sandbox, data = _setup(tmp_path, _RECORD, with_key=False)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    root = tmp_path / "doe"
    (root / "docs" / "fleet").mkdir(parents=True)
    (root / "docs" / "fleet" / "storage-roots.toml").write_text(
        (tmp_path / "fleet" / "storage-roots.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (home / ".claude" / ".coordinator-content-root").write_text(str(root) + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPO_")}
    env["MACHINE_LOCAL_REGISTRY_DIR"] = sandbox
    env["CLAUDE_HOME"] = str(home)
    r = _invoke_ml_inprocess(["locate", "here"], env)
    assert r.returncode == 0 and _row(r)[3] == "true"
    env["CLAUDE_HOME"] = str(tmp_path / "empty-home")
    assert _invoke_ml_inprocess(["locate", "here"], env).returncode == 2


def test_locate_is_read_only(tmp_path):
    sandbox, _ = _setup(tmp_path, _RECORD)
    local = Path(sandbox, "registry.local.toml")
    rec = tmp_path / "fleet" / "storage-roots.toml"
    before = (local.read_bytes(), rec.read_bytes())
    _run_ml(sandbox, ["locate", "here"])
    _run_ml(sandbox, ["locate", "--list"])
    assert (local.read_bytes(), rec.read_bytes()) == before
