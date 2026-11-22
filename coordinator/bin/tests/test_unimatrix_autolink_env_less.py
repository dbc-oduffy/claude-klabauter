"""A login-launched autolink pass (launchd, no COORDINATOR_SETTINGS_HOME) resolves its state dir, and installed
copies that drifted from the klabauter clone are re-copied by the pass itself.

Regression: the Mac mini's autolink died every 60 s with MissingSettingsError and its installed scripts predated
the delivery_settings_home fix, so every Mac->Machine-a send died in the bridge and nothing relinked or refreshed."""
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_state_dir_resolves_without_settings_env(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    roster = _load("unimatrix_roster_envless", "unimatrix-roster.py")
    al = _load("unimatrix_autolink_envless", "unimatrix-autolink.py")
    assert al.state_dir(roster) == roster.delivery_settings_home() / "unimatrix"


def test_autolink_never_calls_strict_settings_home():
    assert "roster.settings_home()" not in (BIN / "unimatrix-autolink.py").read_text(encoding="utf-8")


def test_drift_refreshes_installed_copy_and_throttles(tmp_path):
    al = _load("unimatrix_autolink_drift", "unimatrix-autolink.py")
    src = tmp_path / "clone"
    dest = tmp_path / "home" / "unimatrix"
    src.mkdir()
    dest.mkdir(parents=True)
    (src / "unimatrix-bridge.py").write_text("new\n")
    (dest / "unimatrix-bridge.py").write_text("old\n")
    (src / "unimatrix-install.py").write_text(
        "import shutil, pathlib\n"
        "def install_scripts(s, d):\n"
        "    for p in pathlib.Path(s).glob('unimatrix-*.py'):\n"
        "        shutil.copy2(p, pathlib.Path(d) / p.name)\n")
    assert al.installed_copy_drift(src, dest) == ["unimatrix-bridge.py"]

    class Roster:
        @staticmethod
        def delivery_settings_home():
            return tmp_path / "home"
        settings_home = delivery_settings_home

    clock = [1000.0]
    assert al.refresh_installed_copies(Roster, now=lambda: clock[0], clone_bin=lambda h: src) == []
    assert (dest / "unimatrix-bridge.py").read_text() == "new\n"
    (dest / "unimatrix-bridge.py").write_text("old\n")
    clock[0] += 10
    al.refresh_installed_copies(Roster, now=lambda: clock[0], clone_bin=lambda h: src)
    assert (dest / "unimatrix-bridge.py").read_text() == "old\n"  # inside the throttle window: no re-check
    clock[0] += al.COPIES_CHECK_SECONDS
    al.refresh_installed_copies(Roster, now=lambda: clock[0], clone_bin=lambda h: src)
    assert (dest / "unimatrix-bridge.py").read_text() == "new\n"
