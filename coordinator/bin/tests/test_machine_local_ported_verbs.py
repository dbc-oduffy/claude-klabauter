"""`machine-local` verbs ported from DoE: check, external-workspace, store, relocate-store.

Loads `_machine_local.py` from its file path and drives `main()` in-process against
a tmp_path registry.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"
_spec = importlib.util.spec_from_file_location("_machine_local_ported_verbs_under_test", _IMPL)
_ml = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ml
_spec.loader.exec_module(_ml)

_CASE_FOLDS = os.path.normcase("A") != "A"


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


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _fwd(p: str) -> str:
    return p.replace("\\", "/")


def _lines(r: _R) -> list[list[str]]:
    return [ln.split("\t") for ln in r.stdout.splitlines()]


def _local(sandbox: str) -> str:
    return Path(sandbox, "registry.local.toml").read_bytes().decode()


def test_subcommand_set_is_the_fifteen_doe_names_plus_retire_publisher(monkeypatch):
    seen: dict = {}
    real = argparse.ArgumentParser.parse_args

    def spy(self, *a, **k):
        for act in self._actions:
            if isinstance(act, argparse._SubParsersAction):
                seen["names"] = set(act.choices)
        return real(self, *a, **k)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", spy)
    monkeypatch.setattr(sys, "argv", ["machine-local", "dir"])
    with contextlib.redirect_stdout(io.StringIO()):
        _ml.main()
    doe_names = {
        "get", "has", "keys", "external-workspace", "store", "relocate-store", "dump",
        "check", "path", "dir", "set", "unset", "array-append", "array-set",
        "migrate-publish-mirrors",
    }
    assert seen["names"] == doe_names | {"retire-publisher"}


# ---------------------------------------------------------------- check


def _seed_concern(sandbox: str) -> None:
    _write_file(os.path.join(sandbox, "registry.toml"), 'schema = 1\nconcerns = ["unreal"]\n')


def test_check_reports_shadowed_key_and_load_is_silent(tmp_path):
    sandbox = str(tmp_path / "s")
    _seed_concern(sandbox)
    _write_file(
        os.path.join(sandbox, "registry.local.toml"),
        "[unreal]\nengine_roots = 'x'\n[repos]\nfoo = 'y'\n",
    )
    r = _run_ml(sandbox, ["get", "repos.foo"])
    assert r.returncode == 0 and r.stdout.strip() == "y"
    assert "concern namespace" not in r.stderr
    c = _run_ml(sandbox, ["check"])
    assert c.returncode == 1
    assert "unreal.engine_roots" in c.stdout and "remove" in c.stdout
    assert "unreal.local.toml owns it" in c.stdout


def test_check_clean(tmp_path):
    sandbox = str(tmp_path / "s")
    _seed_concern(sandbox)
    c = _run_ml(sandbox, ["check"])
    assert c.returncode == 0 and "ok" in c.stdout


# ---------------------------------------------------- external-workspace


def _ew_sandbox(tmp_path, local: str) -> tuple[str, str]:
    sandbox = str(tmp_path / "reg")
    root = str(tmp_path / "slice")
    os.makedirs(os.path.join(root, "Source"))
    _write_file(os.path.join(sandbox, "registry.toml"), "schema = 1\n")
    _write_file(
        os.path.join(sandbox, "registry.local.toml"),
        "schema = 1\n" + local.replace("<ROOT>", _fwd(root)),
    )
    return sandbox, root


_EW_ENTRY = (
    "[external_workspaces.p4_fifa_slice]\n"
    'path = "<ROOT>"\nkind = "external-readonly"\nvcs = "p4"\np4_client = "example-operator-mac-slice"\n'
)


def test_ew_lookup_by_key_root_and_nested(tmp_path):
    sandbox, root = _ew_sandbox(tmp_path, _EW_ENTRY)
    want = _fwd(f"p4_fifa_slice\t{root}\texternal-readonly\tp4")
    for arg in ("p4_fifa_slice", root, os.path.join(root, "Source"),
                os.path.join(root, "Source", "not-yet-created.cpp")):
        r = _run_ml(sandbox, ["external-workspace", arg])
        assert r.returncode == 0, (arg, r.stderr)
        assert _fwd(r.stdout.strip()) == want


def test_ew_unregistered_and_sibling_prefix_exit_1(tmp_path):
    sandbox, root = _ew_sandbox(tmp_path, _EW_ENTRY)
    for arg in ("nope", str(tmp_path), root + "-other"):
        assert _run_ml(sandbox, ["external-workspace", arg]).returncode == 1, arg


def test_ew_unknown_kind_reads_as_readonly_and_vcs_defaults(tmp_path):
    sandbox, _ = _ew_sandbox(tmp_path, '[external_workspaces.x]\npath = "<ROOT>"\nkind = "weird"\n')
    r = _run_ml(sandbox, ["external-workspace", "x"])
    assert r.returncode == 0
    assert r.stdout.strip().split("\t")[2:] == ["external-readonly", "none"]


def test_ew_empty_path_is_not_registered(tmp_path):
    sandbox, _ = _ew_sandbox(tmp_path, '[external_workspaces.x]\npath = ""\n')
    assert _run_ml(sandbox, ["external-workspace", "x"]).returncode == 1


def test_ew_list_and_no_argument(tmp_path):
    sandbox, _ = _ew_sandbox(tmp_path, _EW_ENTRY)
    r = _run_ml(sandbox, ["external-workspace", "--list"])
    assert r.returncode == 0 and r.stdout.strip().split("\t")[0] == "p4_fifa_slice"
    empty, _ = _ew_sandbox(tmp_path / "e", "")
    r = _run_ml(empty, ["external-workspace", "--list"])
    assert (r.returncode, r.stdout) == (0, "")
    assert _run_ml(empty, ["external-workspace"]).returncode == 2


def test_ew_case_variant_matches_where_host_folds_case(tmp_path):
    sandbox, root = _ew_sandbox(tmp_path, _EW_ENTRY)
    rc = _run_ml(sandbox, ["external-workspace", root.upper()]).returncode
    assert rc == (0 if _CASE_FOLDS else 1)


def test_ew_malformed_registry_exit_2(tmp_path):
    sandbox, _ = _ew_sandbox(tmp_path, "[[[ not toml")
    r = _run_ml(sandbox, ["external-workspace", "x"])
    assert r.returncode == 2 and "malformed toml" in r.stderr.lower()


def test_set_refuses_repo_inside_workspace(tmp_path):
    sandbox, root = _ew_sandbox(tmp_path, _EW_ENTRY)
    before = _local(sandbox)
    for target in (root, os.path.join(root, "Source")):
        r = _run_ml(sandbox, ["set", "repos.sneaky", target])
        assert r.returncode == 2 and "p4_fifa_slice" in r.stderr
        assert _run_ml(sandbox, ["set", "--dry-run", "repos.sneaky", target]).returncode == 2
    assert _local(sandbox) == before


def test_set_allows_repo_outside_workspace(tmp_path):
    sandbox, _ = _ew_sandbox(tmp_path, _EW_ENTRY)
    other = str(tmp_path / "ordinary")
    os.makedirs(other)
    assert _run_ml(sandbox, ["set", "repos.ordinary", other]).returncode == 0


def test_set_refuses_workspace_path_equal_to_existing_repo(tmp_path):
    sandbox, root = _ew_sandbox(tmp_path, '[repos]\nmine = "<ROOT>"\n')
    r = _run_ml(sandbox, ["set", "external_workspaces.w.path", root])
    assert r.returncode == 2 and "repos.mine" in r.stderr
    other = str(tmp_path / "fresh")
    os.makedirs(other)
    assert _run_ml(sandbox, ["set", "external_workspaces.w.path", other]).returncode == 0
    assert _run_ml(sandbox, ["external-workspace", "w"]).returncode == 0


# ---------------------------------------------------------------- store


def _store_sandbox(tmp_path, local: str, tracked: str = "") -> tuple[str, str]:
    sandbox = str(tmp_path / "reg")
    root = str(tmp_path / "data")
    os.makedirs(os.path.join(root, "sub"))
    _write_file(os.path.join(sandbox, "registry.toml"), "schema = 1\n" + tracked)
    _write_file(
        os.path.join(sandbox, "registry.local.toml"),
        "schema = 1\n" + local.replace("<ROOT>", _fwd(root)),
    )
    return sandbox, root


_REPOS = '[repos]\nexample_stats_repo = "/somewhere/fifa"\n'
_ENTRY = (
    "[stores.facepack]\n"
    'path = "<ROOT>"\nowner = "example_stats_repo"\npurpose = "FIFA facepack renders"\n'
)


def test_store_lookup_by_name_root_and_nested(tmp_path):
    sandbox, root = _store_sandbox(tmp_path, _REPOS + _ENTRY)
    for arg in ("facepack", root, os.path.join(root, "sub"),
                os.path.join(root, "sub", "not-yet-created.bin")):
        r = _run_ml(sandbox, ["store", arg])
        assert r.returncode == 0, (arg, r.stderr)
        f = r.stdout.strip().split("\t")
        assert f[0] == "facepack" and f[2:] == ["example_stats_repo", "FIFA facepack renders"]
        assert _fwd(f[1]) == _fwd(root)
        assert r.stderr == ""


def test_store_deepest_nested_wins(tmp_path):
    inner = '[stores.inner]\npath = "<ROOT>/sub"\nowner = "example_stats_repo"\npurpose = "inner"\n'
    sandbox, root = _store_sandbox(tmp_path, _REPOS + _ENTRY + inner)
    r = _run_ml(sandbox, ["store", os.path.join(root, "sub", "x")])
    assert r.returncode == 0 and r.stdout.split("\t")[0] == "inner"
    assert _run_ml(sandbox, ["store", os.path.join(root, "other")]).stdout.split("\t")[0] == "facepack"


def test_store_unregistered_and_sibling_prefix_exit_1(tmp_path):
    sandbox, root = _store_sandbox(tmp_path, _REPOS + _ENTRY)
    for arg in ("nope", str(tmp_path), root + "-other"):
        assert _run_ml(sandbox, ["store", arg]).returncode == 1, arg


def test_store_case_variant_matches_where_host_folds_case(tmp_path):
    sandbox, root = _store_sandbox(tmp_path, _REPOS + _ENTRY)
    assert _run_ml(sandbox, ["store", root.upper()]).returncode == (0 if _CASE_FOLDS else 1)


def test_store_malformed_registry_and_no_argument_exit_2(tmp_path):
    sandbox, _ = _store_sandbox(tmp_path, "[[[ not toml")
    r = _run_ml(sandbox, ["store", "x"])
    assert r.returncode == 2 and "malformed toml" in r.stderr.lower()
    assert _run_ml(sandbox, ["store", "--check"]).returncode == 2
    ok, _ = _store_sandbox(tmp_path / "ok", _REPOS + _ENTRY)
    assert _run_ml(ok, ["store"]).returncode == 2


def test_store_list(tmp_path):
    second = (
        "[stores.other]\n"
        'path = "<ROOT>/sub"\nowner = "example_stats_repo"\npurpose = "tab\\there\\nnewline"\n'
    )
    sandbox, _ = _store_sandbox(tmp_path, _REPOS + _ENTRY + second)
    r = _run_ml(sandbox, ["store", "--list"])
    assert r.returncode == 0
    rows = _lines(r)
    assert [x[0] for x in rows] == ["facepack", "other"]
    assert all(len(x) == 4 for x in rows)
    assert rows[1][3] == "tab here newline"
    empty, _ = _store_sandbox(tmp_path / "e", "")
    none = _run_ml(empty, ["store", "--list"])
    assert (none.returncode, none.stdout) == (0, "")


def test_store_local_layer_wins_over_tracked(tmp_path):
    tracked = '[stores.facepack]\npath = "/elsewhere"\nowner = "x"\npurpose = "tracked"\n'
    sandbox, root = _store_sandbox(tmp_path, _REPOS + _ENTRY, tracked=tracked)
    f = _run_ml(sandbox, ["store", "facepack"]).stdout.strip().split("\t")
    assert _fwd(f[1]) == _fwd(root) and f[3] == "FIFA facepack renders"


def test_store_path_key_resolves_to_other_keys_value(tmp_path):
    local = (
        '[likeness]\nrender_root = "<ROOT>"\n'
        + _REPOS
        + '[stores.renders]\npath_key = "likeness.render_root"\n'
        'owner = "example_stats_repo"\npurpose = "renders"\n'
    )
    sandbox, root = _store_sandbox(tmp_path, local)
    r = _run_ml(sandbox, ["store", "renders"])
    assert r.returncode == 0, r.stderr
    assert _fwd(r.stdout.split("\t")[1]) == _fwd(root)
    assert _run_ml(sandbox, ["store", os.path.join(root, "sub")]).stdout.split("\t")[0] == "renders"
    assert _run_ml(sandbox, ["store", "--check"]).returncode == 0


def test_store_missing_path_still_resolves_with_stderr_line(tmp_path):
    gone = '[stores.gone]\npath = "<ROOT>/vanished"\nowner = "example_stats_repo"\npurpose = "p"\n'
    sandbox, _ = _store_sandbox(tmp_path, _REPOS + gone)
    r = _run_ml(sandbox, ["store", "gone"])
    assert r.returncode == 0 and r.stdout.split("\t")[0] == "gone"
    assert "gone" in r.stderr and "does not exist" in r.stderr


def test_store_check_clean_exit_0_no_output(tmp_path):
    dep = '\ndependents = ["<ROOT>/sub"]\n'
    sandbox, _ = _store_sandbox(tmp_path, _REPOS + _ENTRY + dep)
    r = _run_ml(sandbox, ["store", "--check"])
    assert (r.returncode, r.stdout) == (0, "")


def test_store_check_emits_each_finding_type(tmp_path):
    local = (
        _REPOS
        + '[stores.m_gone]\npath = "<ROOT>/vanished"\nowner = "example_stats_repo"\npurpose = "p"\n'
        + '[stores.m_keyless]\npath_key = "no.such.key"\nowner = "example_stats_repo"\npurpose = "p"\n'
        + '[stores.o_orphan]\npath = "<ROOT>"\nowner = "ghost_repo"\npurpose = "p"\n'
        + '[stores.o_noowner]\npath = "<ROOT>"\npurpose = "p"\n'
        + '[stores.i_nopurpose]\npath = "<ROOT>"\nowner = "example_stats_repo"\n'
        + '[stores.i_both]\npath = "<ROOT>"\npath_key = "repos.example_stats_repo"\n'
        'owner = "example_stats_repo"\npurpose = "p"\n'
        + '[stores.i_neither]\nowner = "example_stats_repo"\npurpose = "p"\n'
        + '[stores.i_peer]\npath_key = "repos.example_stats_repo"\nowner = "example_stats_repo"\npurpose = "p"\n'
        + '[stores.d_dead]\npath = "<ROOT>"\nowner = "example_stats_repo"\npurpose = "p"\n'
        'dependents = ["<ROOT>/no-such-file.json"]\n'
    )
    sandbox, _ = _store_sandbox(tmp_path, local)
    r = _run_ml(sandbox, ["store", "--check"])
    assert r.returncode == 1, r.stderr
    got = {(x[0], x[1]): x[2] for x in _lines(r)}
    assert got[("missing", "m_gone")].endswith("vanished")
    assert got[("missing", "m_keyless")] == ""
    assert got[("orphan", "o_orphan")] == "ghost_repo"
    assert got[("orphan", "o_noowner")] == ""
    assert got[("incomplete", "i_nopurpose")] == "purpose"
    assert got[("incomplete", "i_both")] == "path/path_key"
    assert got[("incomplete", "i_neither")] == "path/path_key"
    assert got[("incomplete", "i_peer")] == "path_key"
    assert got[("dead-dependent", "d_dead")].endswith("no-such-file.json")
    assert not any(k[1] == "facepack" for k in got)


def test_store_check_and_list_are_exclusive_and_store_is_read_only(tmp_path):
    sandbox, _ = _store_sandbox(tmp_path, _REPOS + _ENTRY)
    before = Path(sandbox, "registry.local.toml").read_bytes()
    assert _run_ml(sandbox, ["store", "--list", "--check"]).returncode == 2
    _run_ml(sandbox, ["store", "--check"])
    _run_ml(sandbox, ["store", "facepack"])
    assert Path(sandbox, "registry.local.toml").read_bytes() == before


# ------------------------------------------------------- relocate-store


def _reloc_setup(tmp_path, local: str, tracked: str = ""):
    sandbox = str(tmp_path / "reg")
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write_file(os.path.join(sandbox, "registry.toml"), "schema = 1\n" + tracked)
    _write_file(
        os.path.join(sandbox, "registry.local.toml"),
        "schema = 1\n" + local.replace("<OLD>", _fwd(str(old))),
    )
    return sandbox, str(old), str(new)


_RBASE = (
    '[repos]\nexample_stats_repo = "/somewhere/fifa"\n'
    "[stores.facepack]\n"
    'path = "<OLD>"\nowner = "example_stats_repo"\npurpose = "renders"\n'
)


def test_relocate_path_store(tmp_path):
    sandbox, _, new = _reloc_setup(tmp_path, _RBASE)
    r = _run_ml(sandbox, ["relocate-store", "facepack", new])
    assert r.returncode == 0, r.stderr
    got = _run_ml(sandbox, ["store", "facepack"])
    assert got.returncode == 0 and _fwd(got.stdout.split("\t")[1]) == _fwd(new)


def test_relocate_path_key_store_rewrites_named_key(tmp_path):
    local = (
        '[repos]\nexample_stats_repo = "/somewhere/fifa"\n'
        '[likeness]\nrender_root = "<OLD>"\n'
        "[stores.renders]\n"
        'path_key = "likeness.render_root"\nowner = "example_stats_repo"\npurpose = "renders"\n'
    )
    sandbox, _, new = _reloc_setup(tmp_path, local)
    r = _run_ml(sandbox, ["relocate-store", "renders", new])
    assert r.returncode == 0, r.stderr
    assert _fwd(_run_ml(sandbox, ["get", "likeness.render_root"]).stdout.strip()) == _fwd(new)
    text = _local(sandbox)
    assert "stores.renders.path" not in text and "path_key" in text
    assert _run_ml(sandbox, ["get", "stores.renders.path"]).returncode != 0


def test_relocate_unknown_name_rc2(tmp_path):
    sandbox, _, new = _reloc_setup(tmp_path, _RBASE)
    before = _local(sandbox)
    assert _run_ml(sandbox, ["relocate-store", "nope", new]).returncode == 2
    assert _local(sandbox) == before


@pytest.mark.parametrize("peer_key", ["repos.example_stats_repo", "stores.other.path", "publish.mirrors.m",
                                      "external_workspaces.w.path"])
def test_relocate_forbidden_path_key_refused_registry_untouched(tmp_path, peer_key):
    local = (
        '[repos]\nexample_stats_repo = "/somewhere/fifa"\n'
        "[stores.bad]\n"
        f'path_key = "{peer_key}"\nowner = "example_stats_repo"\npurpose = "x"\n'
    )
    sandbox, _, new = _reloc_setup(tmp_path, local)
    before = _local(sandbox)
    assert _run_ml(sandbox, ["relocate-store", "bad", new]).returncode == 2
    assert _local(sandbox) == before


def test_relocate_nonexistent_new_path_refused_then_allowed(tmp_path):
    sandbox, _, new = _reloc_setup(tmp_path, _RBASE)
    ghost = os.path.join(new, "not-yet")
    before = _local(sandbox)
    assert _run_ml(sandbox, ["relocate-store", "facepack", ghost]).returncode == 2
    assert _local(sandbox) == before
    r = _run_ml(sandbox, ["relocate-store", "facepack", ghost, "--allow-missing"])
    assert r.returncode == 0, r.stderr
    assert _fwd(_run_ml(sandbox, ["store", "facepack"]).stdout.split("\t")[1]) == _fwd(ghost)


def test_relocate_dry_run_leaves_registry_byte_identical(tmp_path):
    sandbox, _, new = _reloc_setup(tmp_path, _RBASE)
    before = Path(sandbox, "registry.local.toml").read_bytes()
    r = _run_ml(sandbox, ["relocate-store", "facepack", new, "--dry-run"])
    assert r.returncode == 0, r.stderr
    assert "dry-run" in r.stdout
    assert Path(sandbox, "registry.local.toml").read_bytes() == before


def test_relocate_concern_namespace_path_key_refused(tmp_path):
    local = (
        '[repos]\nexample_stats_repo = "/somewhere/fifa"\n'
        "[stores.c]\n"
        'path_key = "unreal.samples_root"\nowner = "example_stats_repo"\npurpose = "x"\n'
    )
    sandbox, _, new = _reloc_setup(tmp_path, local, tracked='concerns = ["unreal"]\n')
    before = _local(sandbox)
    r = _run_ml(sandbox, ["relocate-store", "c", new])
    assert r.returncode == 2 and "--concern" in r.stderr
    assert _local(sandbox) == before


def test_relocate_json_dependent_reported_and_untouched(tmp_path):
    sandbox, old, new = _reloc_setup(tmp_path, _RBASE)
    dep = tmp_path / "projects.json"
    dep.write_bytes(('{"root": "%s"}' % old.replace("\\", "\\\\")).encode())
    clean = tmp_path / "clean.json"
    clean.write_bytes(b'{"root": "/elsewhere"}')
    for f in (dep, clean):
        assert _run_ml(sandbox, ["array-append", "stores.facepack.dependents", str(f)]).returncode == 0
    snap = (dep.read_bytes(), clean.read_bytes())
    r = _run_ml(sandbox, ["relocate-store", "facepack", new])
    assert r.returncode == 0, r.stderr
    lines = [ln.split("\t") for ln in r.stdout.splitlines() if ln.startswith("stale-dependent")]
    assert lines == [["stale-dependent", "facepack", str(dep)]]
    assert (dep.read_bytes(), clean.read_bytes()) == snap


@pytest.mark.skipif(not _CASE_FOLDS, reason="case-insensitive matching only on case-folding hosts")
def test_relocate_case_variant_dependent_reported(tmp_path):
    sandbox, old, new = _reloc_setup(tmp_path, _RBASE)
    dep = tmp_path / "cfg.toml"
    dep.write_text('root = "%s"\n' % old.upper())
    assert _run_ml(sandbox, ["array-append", "stores.facepack.dependents", str(dep)]).returncode == 0
    r = _run_ml(sandbox, ["relocate-store", "facepack", new])
    assert f"stale-dependent\tfacepack\t{dep}" in r.stdout.splitlines()
