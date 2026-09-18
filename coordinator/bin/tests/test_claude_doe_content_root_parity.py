"""Parity pin: `claude-doe.py`'s inline plugin-root probe must agree with its
engine-side twin on every layout.

Purpose: `claude-doe.py` carries a STANDALONE inline mirror of
`coordinator_core.coordinator_root._resolve_plugin_root_for_machine_local`
(`_resolve_plugin_root`) because the launcher is installed by byte copy to
`<settings-home>/bin/claude-doe`, with no `lib/` sibling and no
`coordinator_core` on `sys.path` -- an import there would make the launcher
unlaunchable at exactly the install shape it exists to serve. Unlike the
interpreter ladder pinned by `test_claude_doe_inline_interp_parity.py` this is
not a byte-for-byte copy: the launcher works in `str` and the engine twin
works in `Path`, so a structural `ast` comparison cannot be the pin. This
asserts the property that actually matters instead -- that both resolve the
same layout to the same directory.

Pinned against `_resolve_plugin_root_for_machine_local` specifically, NOT
`coordinator_core.data_root.content_root_for` (claude-klabauter#6 conflict
resolution, 2026-09-18): `content_root_for` accepts a nested `<root>/coordinator`
candidate by `isdir` alone, with no plugin-marker probe, which is looser than
what a machine-local/plugin-dir resolution needs -- `gen_doe_root_pointer.py`
already fails CLOSED on a markerless nested `coordinator/` dir via this same
engine twin, and the launcher holds to that same, stricter bar.

Why it is worth a test rather than a comment: the launcher is in the
interactive launch chain. A drift here does not fail a suite or degrade a
feature -- it refuses to start a session at all, with a remediation describing
whichever layout the stale copy still believed in.

Negative spec:
    - Does NOT compare source text or AST. The two spellings differ by
      design; only the resolved answer is contractual.
    - Does NOT assert the launcher's error message, remediation wording, or
      exit code -- `claude-doe`'s own tests own those.
    - Does NOT touch a real DoE root, the registry, or `$HOME`. Every layout is
      a `tmp_path` fixture.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]
_BIN_LIB = str(_BIN_DIR / "lib")
if _BIN_LIB not in sys.path:
    sys.path.insert(0, _BIN_LIB)

from coordinator_core.coordinator_root import (  # noqa: E402
    _resolve_plugin_root_for_machine_local as engine_resolve_plugin_root,
)


def _load_claude_doe():
    """Import the hyphenated launcher by path -- it is not an importable module
    name, and `main()` is never called here."""
    path = _BIN_DIR / "claude-doe.py"
    spec = importlib.util.spec_from_file_location("_claude_doe_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def claude_doe():
    return _load_claude_doe()


def _nested_marker(tmp_path: Path) -> Path:
    root = tmp_path / "nested-marker"
    impl = root / "coordinator" / "templates" / "bin" / "_machine_local.py"
    impl.parent.mkdir(parents=True)
    impl.write_text("")
    return root


def _flat_marker(tmp_path: Path) -> Path:
    root = tmp_path / "flat-marker"
    impl = root / "templates" / "bin" / "_machine_local.py"
    impl.parent.mkdir(parents=True)
    impl.write_text("")
    return root


def _marketplace_marker(tmp_path: Path) -> Path:
    root = tmp_path / "marketplace-marker"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    return root


def _both_markers(tmp_path: Path) -> Path:
    root = tmp_path / "both-markers"
    impl = root / "coordinator" / "templates" / "bin" / "_machine_local.py"
    impl.parent.mkdir(parents=True)
    impl.write_text("")
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    return root


def _bare(tmp_path: Path) -> Path:
    root = tmp_path / "bare"
    root.mkdir()
    return root


def _unmarked_nested(tmp_path: Path) -> Path:
    # A `coordinator/` subdir with no plugin marker at all -- the case that
    # distinguishes this stricter resolution from `content_root_for`'s
    # isdir-alone acceptance.
    root = tmp_path / "unmarked-nested"
    (root / "coordinator").mkdir(parents=True)
    return root


def _as_str(value) -> str | None:
    return None if value is None else str(value)


@pytest.mark.parametrize(
    "layout, expected_suffix",
    [
        (_nested_marker, "coordinator"),
        (_flat_marker, ""),
        (_marketplace_marker, ""),
        (_both_markers, "coordinator"),
        (_bare, None),
        (_unmarked_nested, None),
    ],
    ids=[
        "nested-dev-clone-marker",
        "flat-oss-clone-marker",
        "flat-marketplace-marker",
        "both-markers-nested-wins",
        "neither",
        "unmarked-nested-coordinator-dir",
    ],
)
def test_launcher_and_engine_twin_agree(claude_doe, tmp_path, layout, expected_suffix) -> None:
    root = layout(tmp_path)

    launcher = claude_doe._resolve_plugin_root(str(root))
    engine = _as_str(engine_resolve_plugin_root(root))

    assert launcher == engine, {
        "launcher": launcher,
        "coordinator_core.coordinator_root": engine,
    }
    if expected_suffix is None:
        assert launcher is None
    else:
        assert launcher == str(root / expected_suffix) if expected_suffix else launcher == str(root)


def test_the_empty_root_is_not_a_plugin_root_in_either_spelling(claude_doe) -> None:
    """A root that did not resolve must not silently become the cwd-relative
    string `"coordinator"`, which is what a bare join on an empty value
    produces."""
    assert claude_doe._resolve_plugin_root("") is None
    assert engine_resolve_plugin_root(Path("")) is None


def test_the_flat_arm_is_gated_on_the_same_marker_in_both(claude_doe, tmp_path) -> None:
    """A directory is the flat mirror only because its plugin manifest says so.
    A shared marker that drifted in one spelling would trust a directory the
    other rejects -- the failure mode the named marker exists to prevent."""
    root = tmp_path / "manifest-missing"
    (root / ".claude-plugin").mkdir(parents=True)

    assert claude_doe._resolve_plugin_root(str(root)) is None
    assert engine_resolve_plugin_root(root) is None
