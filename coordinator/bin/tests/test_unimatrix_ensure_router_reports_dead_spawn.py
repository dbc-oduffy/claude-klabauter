"""A supervisor-spawned router that dies in start() is reported with its exit code and stderr, never silently respawned."""
import importlib.util
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("mirror_all_under_test", BIN / "unimatrix-mirror-all.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ensure_router_names_why_the_spawned_router_exited(tmp_path, monkeypatch):
    mod = _load()
    roster = mod._roster()
    mod._router_module()
    (tmp_path / "unimatrix-peer-router.py").write_text(
        "import sys\nsys.stderr.write('AttributeError: boom in start\\n')\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(mod, "HERE", tmp_path)
    monkeypatch.setattr(mod, "_roster", lambda: roster)
    monkeypatch.setattr(roster, "delivery_settings_home", lambda: tmp_path)
    monkeypatch.setattr(roster, "console_python", lambda: sys.executable)
    monkeypatch.setattr(mod, "router_answers", lambda ctl: False)
    monkeypatch.setattr(mod, "kickstart_router_agent", lambda say: False)
    monkeypatch.delenv("UNIMATRIX_ROUTER_SUPERVISION", raising=False)
    said = []
    assert mod.ensure_router(said.append, wait=5.0) is True
    joined = "\n".join(said)
    assert "exited rc=3" in joined and "boom in start" in joined, said
