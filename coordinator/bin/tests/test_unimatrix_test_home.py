"""The default autouse isolation yields a Unimatrix test home that reaches nothing real."""
import importlib.util
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    return _load(BIN / "lib" / "unimatrix_test_home.py", "unimatrix_test_home_t")


def test_default_isolation_is_a_test_home(guard):
    assert guard.is_test_home()
    assert not guard.is_test_home({})
    assert not guard.is_test_home({"UNIMATRIX_TEST_HOME": "0"})


def test_copied_registry_local_carries_no_unimatrix_key():
    home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    local = home / "machine-local" / "registry.local.toml"
    if not local.is_file():
        pytest.skip("box has no registry.local.toml to copy")
    for line in local.read_text(encoding="utf-8").splitlines():
        assert not line.lstrip().lstrip("\"'").startswith("unimatrix.")


def test_strip_removes_keys_and_tables_but_keeps_the_rest(guard):
    text = (
        'a = 1\n"unimatrix.far_boxes" = "x@192.168.0.1"\nunimatrix.keys.b = "k"\n'
        '[unimatrix]\nfar_boxes = "z"\n[other]\nc = 2\n'
    )
    out = guard.strip_unimatrix_keys(text)
    assert out == "a = 1\n[other]\nc = 2\n"


def test_refuse_real_key_path(guard):
    home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    guard.refuse_real("key_path", home / "k" / "id")
    with pytest.raises(guard.TestHomeRefusal) as e:
        guard.refuse_real("key_path", Path.home() / ".ssh" / "id_ed25519")
    assert e.value.kind == "key_path"
    with pytest.raises(guard.TestHomeRefusal):
        guard.refuse_real("key_path", home.parent / "elsewhere" / "id")


def test_refuse_real_far_box(guard, monkeypatch):
    with pytest.raises(guard.TestHomeRefusal):
        guard.refuse_real("far_box", "example-operator@192.168.0.174")
    with pytest.raises(guard.TestHomeRefusal):
        guard.refuse_real("far_box", "macmini.local")
    guard.refuse_real("far_box", "u@127.0.0.1")
    guard.refuse_real("far_box", "localhost")
    monkeypatch.setenv("UNIMATRIX_TEST_ALLOWED_BOXES", "fake-box other-box")
    guard.refuse_real("far_box", "u@fake-box")
    with pytest.raises(guard.TestHomeRefusal):
        guard.refuse_real("far_box", "u@real-box")


def test_refuse_real_run_key(guard):
    with pytest.raises(guard.TestHomeRefusal) as e:
        guard.refuse_real("run_key", "UnimatrixAutolink")
    assert e.value.kind == "run_key"


def test_refuse_real_is_a_noop_outside_a_test_home(guard):
    guard.refuse_real("run_key", "x", env={})
    guard.refuse_real("far_box", "u@192.168.0.1", env={})


def test_unknown_kind_is_an_error(guard):
    with pytest.raises(ValueError):
        guard.refuse_real("bogus", "x")


def test_agy_and_peer_paths_resolve_under_the_test_home():
    home = Path(os.environ["COORDINATOR_SETTINGS_HOME"]).resolve()
    router = _load(BIN / "unimatrix-peer-router.py", "unimatrix_peer_router_t")
    for p in (router.default_presence_dir(), router.default_legs_dir(), router.default_peers_file()):
        assert home in Path(p).resolve().parents, p
