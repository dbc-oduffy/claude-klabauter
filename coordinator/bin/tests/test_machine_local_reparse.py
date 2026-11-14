"""`machine-local store --check` reparse finding: a directory junction is caught, a volume-GUID
mount and the engine's own links pass. Loads `_machine_local.py` from its file path."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.complete

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"
_spec = importlib.util.spec_from_file_location("_machine_local_reparse_under_test", _IMPL)
_ml = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ml
_spec.loader.exec_module(_ml)

_NT = os.name == "nt"
_SHA = "a" * 40


def _fwd(p) -> str:
    return str(p).replace("\\", "/")


def _link_dir(link: Path, target: Path) -> None:
    """A real directory junction on nt, a directory symlink on POSIX."""
    if _NT:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink not creatable here: {exc}")


def _run_check(tmp_path: Path, root: Path, extra: str = "") -> tuple[int, str]:
    reg = tmp_path / "reg"
    reg.mkdir(exist_ok=True)
    (reg / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    (reg / "registry.local.toml").write_text(
        "schema = 1\n"
        f"{extra}\n"
        '[repos]\nowner_repo = "/somewhere"\n'
        f'[stores.s]\npath = "{_fwd(root)}"\nowner = "owner_repo"\npurpose = "p"\n',
        encoding="utf-8",
    )
    old_env, old_argv = dict(os.environ), sys.argv
    out = io.StringIO()
    code = 0
    try:
        os.environ["MACHINE_LOCAL_REGISTRY_DIR"] = str(reg)
        sys.argv = ["machine-local", "store", "--check"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                result = _ml.main()
                code = result if isinstance(result, int) else 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 0
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.argv = old_argv
    return code, out.getvalue()


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    return root


def test_junction_is_caught_with_path_and_target(tmp_path, store):
    link = store / "sub" / "mh-neutral-export"
    _link_dir(link, tmp_path / "elsewhere")
    code, out = _run_check(tmp_path, store)
    assert code == 1
    kind, name, detail = out.rstrip("\n").split("\t")
    assert (kind, name) == ("reparse", "s")
    path, _, target = detail.partition(" -> ")
    assert _fwd(path) == _fwd(link)
    assert "elsewhere" in target
    expected = "junction" if _NT else "symlink"
    assert _ml.classify_reparse(str(link)) == expected


def test_clean_store_passes(tmp_path, store):
    assert _run_check(tmp_path, store) == (0, "")


def test_not_descended_into_the_reparse_point(tmp_path, store):
    inner = tmp_path / "elsewhere" / "inner-target"
    inner.mkdir()
    _link_dir(tmp_path / "elsewhere" / "nested", inner)
    _link_dir(store / "hop", tmp_path / "elsewhere")
    code, out = _run_check(tmp_path, store)
    assert code == 1 and len(out.splitlines()) == 1


def test_allowlisted_path_passes_and_unlisted_still_refused(tmp_path, store):
    link = store / "allowed"
    _link_dir(link, tmp_path / "elsewhere")
    other = store / "other"
    _link_dir(other, tmp_path / "elsewhere")
    extra = f'reparse_allowlist = ["{_fwd(link)}"]'
    code, out = _run_check(tmp_path, store, extra)
    assert code == 1
    assert _fwd(other) in _fwd(out) and _fwd(link) not in _fwd(out)


def _nt_stat(tag: int, attrs: int = 0x400) -> SimpleNamespace:
    return SimpleNamespace(st_file_attributes=attrs, st_reparse_tag=tag, st_mode=0o040777)


@pytest.mark.parametrize("target", [
    "\\??\\Volume{12345678-1234-1234-1234-123456789abc}\\",
    "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\",
    "\\??\\VOLUME{12345678-1234-1234-1234-123456789abc}\\",
])
def test_volume_guid_mount_target_passes(monkeypatch, target):
    monkeypatch.setattr(_ml, "_host_is_nt_override", lambda: True)
    monkeypatch.setattr(_ml.os, "readlink", lambda p: target)
    assert _ml._classify_stat(_nt_stat(_ml._TAG_MOUNT_POINT), "mnt") == ("volume-mount", target)


def test_volume_mount_is_not_a_finding_in_scan(tmp_path, monkeypatch):
    (tmp_path / "mnt").mkdir()
    monkeypatch.setattr(_ml, "_host_is_nt_override", lambda: True)
    monkeypatch.setattr(_ml.os, "readlink", lambda p: "\\??\\Volume{1-2}\\")
    real_scandir = _ml.os.scandir

    class _E:
        def __init__(self, e):
            self.name, self.path = e.name, e.path

        def is_dir(self, follow_symlinks=True):
            return True

        def is_symlink(self):
            return False

        def stat(self, follow_symlinks=True):
            return _nt_stat(_ml._TAG_MOUNT_POINT)

    class _It:
        def __init__(self, it):
            self.it = it

        def __enter__(self):
            return self

        def __iter__(self):
            return (_E(e) for e in self.it)

        def __exit__(self, *a):
            self.it.close()

    with monkeypatch.context() as m:
        m.setattr(_ml.os, "scandir", lambda p: _It(real_scandir(p)))
        assert _ml.scan_reparse_points([str(tmp_path)], []) == []


def test_directory_path_mount_point_target_is_a_junction(monkeypatch):
    monkeypatch.setattr(_ml, "_host_is_nt_override", lambda: True)
    monkeypatch.setattr(_ml.os, "readlink", lambda p: "\\??\\target-dir")
    assert _ml._classify_stat(_nt_stat(_ml._TAG_MOUNT_POINT), "x")[0] == "junction"


def test_nt_ungoverned_reparse_tags_and_plain_dirs_are_none(monkeypatch):
    monkeypatch.setattr(_ml, "_host_is_nt_override", lambda: True)
    assert _ml._classify_stat(_nt_stat(0x9000001A), "x")[0] is None
    assert _ml._classify_stat(_nt_stat(0, attrs=0x10), "x")[0] is None


def test_chain_export_engine_links_pass(tmp_path):
    export = tmp_path / "claude-klabauter-publish-chain" / _SHA
    (export / "coordinator").mkdir(parents=True)
    engine = tmp_path / "engine-export"
    (engine / "coordinator_core").mkdir(parents=True)
    (engine / "coordinator" / "lib").mkdir(parents=True)
    _symlink_or_skip(export / "coordinator_core", engine / "coordinator_core")
    _symlink_or_skip(export / "coordinator" / "lib", engine / "coordinator" / "lib")
    assert _ml.scan_reparse_points([str(tmp_path / "claude-klabauter-publish-chain")], []) == []


def test_chain_export_link_outside_a_sha_export_is_caught(tmp_path):
    other = tmp_path / "somewhere" / "coordinator_core"
    other.parent.mkdir()
    _symlink_or_skip(other, tmp_path)
    assert [p for p, _ in _ml.scan_reparse_points([str(tmp_path / "somewhere")], [])] == [str(other)]


def test_generation_swap_link_passes_and_other_sibling_is_caught(tmp_path, store):
    gen = store / "venv.gen-123-abcd1234"
    gen.mkdir()
    _link_dir(store / "venv", gen)
    assert _run_check(tmp_path, store) == (0, "")
    stranger = store / "stranger"
    stranger.mkdir()
    _link_dir(store / "venv2", stranger)
    code, out = _run_check(tmp_path, store)
    assert code == 1 and "venv2" in out


def test_engine_link_mirror_matches_percolate_round_source():
    """The template's hand mirror of percolate-round.py's chain-export constants must not drift:
    a drifted tuple would turn the engine's own links into findings."""
    import ast

    src = (Path(__file__).resolve().parents[1] / "percolate-round.py").read_text(encoding="utf-8")
    consts = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            consts[node.targets[0].id] = node.value
    assert ast.literal_eval(consts["_ENGINE_LINKED_PATHS"]) == _ml._ENGINE_LINKED_PATHS
    assert ast.literal_eval(consts["_CHAIN_CACHE_DIRNAME"]) == _ml._CHAIN_CACHE_DIRNAME
    assert ast.get_source_segment(src, consts["_FULL_SHA_RE"]) == (
        're.compile(r"' + _ml._FULL_SHA_RE.pattern + '")'
    )


def test_store_whose_effective_path_is_the_junction_is_caught(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "mh-neutral-export"
    _link_dir(link, real)
    code, out = _run_check(tmp_path, link)
    assert code == 1
    kind, name, detail = out.rstrip("\n").split("\t")
    assert (kind, name) == ("reparse", "s")
    assert _fwd(detail.partition(" -> ")[0]) == _fwd(link)


def test_store_root_that_is_allowlisted_or_volume_mount_passes(tmp_path, monkeypatch):
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "mh-neutral-export"
    _link_dir(link, real)
    assert _ml.scan_reparse_points([link], [str(link)], judge_roots=True) == []
    assert _ml.scan_reparse_points([link], [], judge_roots=True) != []
    monkeypatch.setattr(_ml, "_classify_stat", lambda st, path: ("volume-mount", r"\??\Volume{x}\\"))
    assert _ml.scan_reparse_points([link], [], judge_roots=True) == []


def test_classify_reparse_none_for_plain_and_absent(tmp_path):
    assert _ml.classify_reparse(str(tmp_path)) is None
    assert _ml.classify_reparse(str(tmp_path / "nope")) is None
