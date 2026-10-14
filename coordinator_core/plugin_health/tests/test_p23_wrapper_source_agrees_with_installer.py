"""Doctor P-23 and the Step 3.5b installer resolve one claude-author wrapper source."""

import os
from pathlib import Path

import pytest

from coordinator_core.install import maximalist
from coordinator_core.install._shared import claude_author_wrapper_source
from coordinator_core.ops import install_claude_author_wrapper
from coordinator_core.plugin_health import sentinel


def _write_wrapper(root: Path, body: str) -> Path:
    path = root / "coordinator" / "bin" / "claude-author.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    settings = tmp_path / "settings"
    registry = tmp_path / "registry"
    for d in (home, settings / "bin", registry):
        d.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    live = tmp_path / "live"
    _write_wrapper(live, "# live tree wrapper\n")
    return {
        "home": home,
        "settings_bin": settings / "bin",
        "registry": registry,
        "live": live,
        "tmp": tmp_path,
    }


def _publish(box, body="# published wrapper\n") -> Path:
    root = box["tmp"] / "published"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_bytes(b"stamp")
    src = _write_wrapper(root, body)
    posix_root = root.as_posix()
    (box["registry"] / "registry.local.toml").write_text(
        f'[repos]\nclaude_klabauter = "{posix_root}"\n', encoding="utf-8"
    )
    return src


def _install(box):
    maximalist._install_claude_author_wrapper(
        coord_root=str(box["tmp"] / "doe"),
        claude_home_dir=str(box["home"]),
        check_only=False,
        orch=maximalist._Orchestrator(),
        claude_klabauter_root=str(box["live"]),
        settings_bin=str(box["settings_bin"]),
    )


def _probe(box):
    return sentinel.probe_p23(box["live"], box["home"], box["settings_bin"])


def test_published_build_wins_and_installer_and_probe_agree(box):
    published = _publish(box)
    assert claude_author_wrapper_source(box["live"]) == published
    _install(box)
    assert _probe(box) == []
    dst = box["home"] / ".local" / "bin" / "claude-author"
    assert dst.resolve().read_bytes() == published.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="byte-copy drift only exists on the Windows branch")
def test_drift_is_amber_with_runnable_remedy_and_remedy_clears_it(box):
    published = _publish(box)
    _install(box)
    dst = box["home"] / ".local" / "bin" / "claude-author"
    dst.write_text("# edited\n", encoding="utf-8")

    notes = _probe(box)
    assert [n.severity for n in notes] == ["amber"]
    assert f"install-claude-author-wrapper --wrapper-src {published}" in notes[0].message
    assert "scripts/setup.py" not in notes[0].message

    assert install_claude_author_wrapper.main(["--wrapper-src", str(published)]) == 0
    assert _probe(box) == []


def test_no_published_root_falls_back_to_own_root_on_both_sides(box):
    live_src = box["live"] / "coordinator" / "bin" / "claude-author.py"
    assert claude_author_wrapper_source(box["live"]) == live_src
    _install(box)
    assert _probe(box) == []
    dst = box["home"] / ".local" / "bin" / "claude-author"
    assert dst.resolve().read_bytes() == live_src.read_bytes()


def test_unstamped_published_root_is_ignored(box):
    published = _publish(box)
    (published.parents[2] / "coordinator_core" / "_engine_stamp").unlink()
    assert claude_author_wrapper_source(box["live"]) == (
        box["live"] / "coordinator" / "bin" / "claude-author.py"
    )
