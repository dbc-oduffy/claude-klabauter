"""`klabauter-reconcile.py::_published_paths` must find the round manifest at
the mirror root when the target's dest dir sits below it -- klabauter's dest
resolves to `<mirror>/coordinator_core`, and reading the manifest there
refused every `--take-candidate` resolution as unprovable."""

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "klabauter-reconcile.py"


def _load():
    spec = importlib.util.spec_from_file_location("klabauter_reconcile", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_manifest(root: Path) -> None:
    (root / ".percolate").mkdir()
    (root / ".percolate" / "round-manifest.json").write_text(
        json.dumps({"added_or_updated": ["coordinator_core/_engine_stamp", "scripts/cloud_setup.py"]}),
        encoding="utf-8",
    )


def test_dest_below_mirror_root_reads_the_root_manifest(tmp_path):
    _write_manifest(tmp_path)
    dest = tmp_path / "coordinator_core"
    dest.mkdir()

    paths = _load()._published_paths(str(dest))

    assert paths == {"coordinator_core/_engine_stamp", "scripts/cloud_setup.py"}


def test_dest_at_mirror_root_reads_its_manifest(tmp_path):
    _write_manifest(tmp_path)

    assert "scripts/cloud_setup.py" in _load()._published_paths(str(tmp_path))


def test_no_manifest_anywhere_refuses(tmp_path):
    dest = tmp_path / "coordinator_core"
    dest.mkdir()

    assert _load()._published_paths(str(dest)) is None
