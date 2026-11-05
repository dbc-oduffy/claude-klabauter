"""`publish.py :: _source_map_shadowed_files` and its end-of-run attribution: a `source_map` override
that drops row-source bin/lib files is printed per row, and the plugin-payload gate names the
dropped file when the payload imports it or a doc references it.

Run: python -m pytest coordinator/bin/tests/test_publish_source_map_shadow_gate.py -q
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_source_map_shadow_gate_under_test", "publish.py")


def _write(path: Path, text: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _trees(tmp_path: Path):
    doe, engine = tmp_path / "doe", tmp_path / "engine"
    _write(doe / "lib" / "output_tree_layout.py")
    _write(doe / "lib" / "shared.py")
    _write(doe / "lib" / "__pycache__" / "shared.cpython-311.pyc")
    _write(doe / "bin" / "tool.py")
    _write(engine / "lib" / "shared.py")
    _write(engine / "bin" / "tool.py")
    return doe, engine


def _target(doe: Path, source_map: str, name: str = "coordinator-claude"):
    return publish.ResolvedTarget(name=name, mode="mirror", source_dir=doe,
                                  dest_dir=doe.parent / "dest", source_map=source_map)


def test_dropped_files_are_listed(tmp_path):
    doe, engine = _trees(tmp_path)
    assert publish._source_map_shadowed_files(_target(doe, f"{engine}=bin,lib")) == [
        "lib/output_tree_layout.py"
    ]


def test_single_source_lists_nothing_and_prints_nothing(tmp_path, capsys):
    doe, _engine = _trees(tmp_path)
    target = _target(doe, "")
    assert publish._source_map_shadowed_files(target) == []
    publish.assert_authored_parity(target)
    assert capsys.readouterr().out == ""


def test_entry_routed_to_own_source_drops_nothing(tmp_path):
    doe, engine = _trees(tmp_path)
    assert publish._source_map_shadowed_files(_target(doe, f"{doe}=lib;{engine}=bin")) == []


def test_assert_authored_parity_prints_the_dropped_list_without_refusing(tmp_path, capsys):
    doe, engine = _trees(tmp_path)
    publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))
    assert "lib/output_tree_layout.py" in capsys.readouterr().out


def _run_gate(tmp_path: Path, monkeypatch, docs, imports):
    publish._bootstrap_engine()
    payload = tmp_path / "mirror"
    payload.mkdir()
    monkeypatch.setattr(publish, "check_plugin_payload", lambda *a, **k: (docs, imports, None))
    row = publish.ResolvedTarget(name="coordinator-claude", mode="mirror", source_dir=tmp_path / "doe",
                                 dest_dir=payload)
    out, err = io.StringIO(), io.StringIO()
    ok = publish.dispatch_end_of_run_plugin_payload_gate(
        [payload], rows_by_repo_root={payload: [row]}, err=err, out=out
    )
    return ok, err.getvalue()


def test_dropped_and_imported_file_fails_and_names_it(tmp_path, monkeypatch):
    doe, engine = _trees(tmp_path)
    publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))
    ok, err = _run_gate(tmp_path, monkeypatch, [], [("hooks/h.py", "output_tree_layout")])
    assert ok is False
    assert "lib/output_tree_layout.py: imported by hooks/h.py" in err
    assert "carry the file into the source_map root" in err


def test_dropped_and_doc_referenced_file_fails_and_names_it(tmp_path, monkeypatch):
    doe, engine = _trees(tmp_path)
    publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))
    ok, err = _run_gate(tmp_path, monkeypatch, [("README.md", "lib/output_tree_layout.py")], [])
    assert ok is False
    assert "lib/output_tree_layout.py: referenced by README.md" in err


def test_dropped_but_unreferenced_file_passes_and_prints(tmp_path, monkeypatch, capsys):
    doe, engine = _trees(tmp_path)
    publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))
    assert "lib/output_tree_layout.py" in capsys.readouterr().out
    ok, err = _run_gate(tmp_path, monkeypatch, [], [])
    assert ok is True
    assert err == ""


def test_unrelated_failure_is_not_attributed_to_a_shadowed_file(tmp_path, monkeypatch):
    doe, engine = _trees(tmp_path)
    publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))
    ok, err = _run_gate(tmp_path, monkeypatch, [], [("hooks/h.py", "other_module")])
    assert ok is False
    assert "source_map shadowing" not in err
