"""`_machine_local.py` DoE-parity deltas D4-D9, pinned through the CLI `main()` entry.

Loads the implementation from its file path and drives `main()` in-process against a
tmp_path registry.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"
_spec = importlib.util.spec_from_file_location("_machine_local_small_deltas_under_test", _IMPL)
_ml = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ml
_spec.loader.exec_module(_ml)

_UNPROVISIONED = "is registered in concerns=[...]"


class _R:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _run_ml(sandbox: str, argv: list[str]) -> _R:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPO_")}
    env["MACHINE_LOCAL_REGISTRY_DIR"] = sandbox
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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fwd(p: str) -> str:
    return p.replace("\\", "/")


def _sandbox(tmp_path: Path, local: str = "", tracked: str = "") -> str:
    reg = tmp_path / "reg"
    _write(reg / "registry.toml", "schema = 1\n" + tracked)
    _write(reg / "registry.local.toml", "schema = 1\n" + local)
    return str(reg)


def _unprovisioned_sandbox(tmp_path: Path) -> str:
    reg = tmp_path / "reg"
    _write(reg / "registry.toml", 'schema = 1\nconcerns = ["ghost"]\n')
    _write(reg / "registry.local.toml", 'schema = 1\n[repos]\nfoo = "x"\n')
    return str(reg)


# ---------------------------------------------------------------- D4


def test_set_global_repos_refused_with_fix_it_and_registry_untouched(tmp_path):
    sandbox = _sandbox(tmp_path)
    before = (Path(sandbox) / "registry.toml").read_bytes()
    r = _run_ml(sandbox, ["set", "--global", "repos.x", str(tmp_path)])
    assert r.returncode == 1
    assert "refusing `set --global repos.x`" in r.stderr
    assert "machine-local set repos.x <path>" in r.stderr
    assert (Path(sandbox) / "registry.toml").read_bytes() == before


def test_set_local_repos_and_global_non_repos_still_proceed(tmp_path):
    sandbox = _sandbox(tmp_path)
    target = tmp_path / "somerepo"
    target.mkdir()
    assert _run_ml(sandbox, ["set", "repos.x", str(target)]).returncode == 0
    assert _run_ml(sandbox, ["set", "--global", "demo.flag", "on"]).returncode == 0


# ---------------------------------------------------------------- D5


def test_get_and_set_of_repos_keys_do_not_warn_unprovisioned(tmp_path):
    sandbox = _unprovisioned_sandbox(tmp_path)
    r = _run_ml(sandbox, ["get", "repos.foo"])
    assert r.returncode == 0 and r.stdout.strip() == "x"
    assert _UNPROVISIONED not in r.stderr
    other = tmp_path / "other"
    other.mkdir()
    r = _run_ml(sandbox, ["set", "repos.bar", str(other)])
    assert r.returncode == 0
    assert _UNPROVISIONED not in r.stderr


def test_keys_warns_unprovisioned_concern(tmp_path):
    sandbox = _unprovisioned_sandbox(tmp_path)
    r = _run_ml(sandbox, ["keys"])
    assert r.returncode == 0
    assert "repos.foo" in r.stdout.split()
    assert "concern 'ghost'" in r.stderr and _UNPROVISIONED in r.stderr


# ---------------------------------------------------------------- D6


def test_external_fields_accept_kind_and_p4_client():
    assert _ml._EXTERNAL_FIELDS == ("path", "kind", "vcs", "p4_client")


def test_external_workspace_with_kind_and_p4_client_resolves(tmp_path):
    root = tmp_path / "slice"
    root.mkdir()
    sandbox = _sandbox(
        tmp_path,
        local=(
            "[external_workspaces.slice]\n"
            f'path = "{_fwd(str(root))}"\nkind = "external-readonly"\n'
            'vcs = "p4"\np4_client = "client-a"\n'
        ),
    )
    r = _run_ml(sandbox, ["external-workspace", "slice"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().split("\t")[2:] == ["external-readonly", "p4"]
    keys = _run_ml(sandbox, ["keys"]).stdout.split()
    assert "external_workspaces.slice.p4_client" in keys


# ---------------------------------------------------------------- D7


def test_collision_refusal_names_workspace_never_under_a_repo(tmp_path):
    repo = tmp_path / "peer"
    repo.mkdir()
    sandbox = _sandbox(tmp_path, local=f'[repos]\nmine = "{_fwd(str(repo))}"\n')
    r = _run_ml(sandbox, ["set", "external_workspaces.w.path", str(repo)])
    assert r.returncode == 2
    assert "repos.mine" in r.stderr
    assert "a workspace never lives under a repo's tree" in r.stderr


# ---------------------------------------------------------------- D8 / D9


def test_file_starts_with_shebang_and_carries_future_import():
    text = _IMPL.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3\n")
    assert "\nfrom __future__ import annotations\n" in text


def test_future_import_precedes_other_imports():
    lines = _IMPL.read_text(encoding="utf-8").splitlines()
    future = lines.index("from __future__ import annotations")
    first_import = next(i for i, ln in enumerate(lines) if ln.startswith("import "))
    assert future < first_import


@pytest.mark.skipif(os.name == "nt", reason="the working-tree exec bit is not tracked on Windows")
def test_file_is_executable_on_posix():
    assert _IMPL.stat().st_mode & stat.S_IXUSR
