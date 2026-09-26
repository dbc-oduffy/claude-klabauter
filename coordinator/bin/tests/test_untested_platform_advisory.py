from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import unittest.mock
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_HELPER_PATH = _BIN_DIR / "untested-platform-advisory.py"


def _load_module(path: Path, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_helper = _load_module(_HELPER_PATH, "untested_platform_advisory")


def _manifest(*, declared=True, present=None, tested=None) -> dict:
    m: dict = {}
    if declared is not None:
        m["packageability_compliance"] = {"declared": declared}
    if present is not None:
        m["present_platforms"] = present
    if tested is not None:
        m["tested_platforms"] = tested
    return m


def test_present_not_tested_emits_exactly_one_line() -> None:
    manifest = _manifest(declared=True, present=["macos", "linux", "windows"],
                          tested=["macos", "linux"])
    line = _helper.advisory_line(manifest, "windows")
    assert line is not None, "present-but-untested must emit an advisory line"
    assert isinstance(line, str)
    assert "\n" not in line, "must be exactly ONE line — no embedded newlines"
    assert "windows" in line, "advisory must name the running platform"
    assert 'agent-install-contract.md\'s "### Point 4" subsection' in line, (
        "advisory must point at the Point 4 subsection by heading"
    )


def test_tested_platform_is_silent() -> None:
    manifest = _manifest(declared=True, present=["macos", "linux", "windows"],
                          tested=["macos", "linux", "windows"])
    line = _helper.advisory_line(manifest, "windows")
    assert line is None, "tested platform must be silent"


def test_no_packageability_marker_is_silent() -> None:
    manifest = _manifest(declared=None, present=["macos", "linux", "windows"],
                          tested=["macos", "linux"])
    line = _helper.advisory_line(manifest, "windows")
    assert line is None, "no packageability_compliance marker must be silent"


def test_declared_false_is_silent() -> None:
    manifest = _manifest(declared=False, present=["windows"], tested=[])
    line = _helper.advisory_line(manifest, "windows")
    assert line is None, "declared:false must be silent (not opted in)"


def test_platform_absent_from_both_arrays_is_silent() -> None:
    manifest = _manifest(declared=True, present=["macos", "linux"],
                          tested=["macos", "linux"])
    line = _helper.advisory_line(manifest, "windows")
    assert line is None, "platform absent from present_platforms must be silent"


def test_missing_manifest_is_silent() -> None:
    line = _helper.advisory_line(None, "windows")
    assert line is None, "unreadable/missing manifest must be silent, not raise"


def test_unrecognized_running_platform_is_silent() -> None:
    manifest = _manifest(declared=True, present=["macos", "linux", "windows"],
                          tested=["macos", "linux"])
    line = _helper.advisory_line(manifest, None)
    assert line is None, "unresolved running platform must be silent, not raise"


def test_darwin_maps_to_macos() -> None:
    with unittest.mock.patch.object(_helper.platform, "system", return_value="Darwin"):
        assert _helper._running_platform_id() == "macos"


def test_linux_maps_to_linux() -> None:
    with unittest.mock.patch.object(_helper.platform, "system", return_value="Linux"):
        assert _helper._running_platform_id() == "linux"


def test_windows_maps_to_windows() -> None:
    with unittest.mock.patch.object(_helper.platform, "system", return_value="Windows"):
        assert _helper._running_platform_id() == "windows"


def test_unrecognized_platform_maps_to_none() -> None:
    with unittest.mock.patch.object(_helper.platform, "system", return_value="OpenBSD"):
        assert _helper._running_platform_id() is None


def _run_main_with_manifest(tmp_path, manifest: dict, running_platform):
    manifest_path = str(tmp_path / "agent-install-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    captured_out = io.StringIO()
    with (
        unittest.mock.patch.object(_helper, "_manifest_path", return_value=manifest_path),
        unittest.mock.patch.object(_helper, "_running_platform_id", return_value=running_platform),
        unittest.mock.patch("sys.stdout", captured_out),
    ):
        rc = _helper.main()
    return rc, captured_out.getvalue()


def test_present_not_tested_end_to_end(tmp_path) -> None:
    manifest = _manifest(declared=True, present=["macos", "linux", "windows"],
                          tested=["macos", "linux"])
    rc, out = _run_main_with_manifest(tmp_path, manifest, "windows")
    assert rc == 0, "must always exit 0 (advisory only)"
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1, f"must emit exactly one line, got: {lines!r}"
    assert "windows" in lines[0]


def test_tested_end_to_end_silent(tmp_path) -> None:
    manifest = _manifest(declared=True, present=["macos", "linux", "windows"],
                          tested=["macos", "linux", "windows"])
    rc, out = _run_main_with_manifest(tmp_path, manifest, "windows")
    assert rc == 0
    assert out == "", "tested platform must emit nothing"


def test_no_marker_end_to_end_silent(tmp_path) -> None:
    manifest = _manifest(declared=None, present=["macos", "linux", "windows"],
                          tested=["macos", "linux"])
    rc, out = _run_main_with_manifest(tmp_path, manifest, "windows")
    assert rc == 0
    assert out == "", "no packageability_compliance marker must emit nothing"


def test_missing_manifest_file_end_to_end_silent_exit0(tmp_path) -> None:
    captured_out = io.StringIO()
    with (
        unittest.mock.patch.object(
            _helper, "_manifest_path",
            return_value=str(tmp_path / "does-not-exist.json"),
        ),
        unittest.mock.patch.object(_helper, "_running_platform_id", return_value="windows"),
        unittest.mock.patch("sys.stdout", captured_out),
    ):
        rc = _helper.main()
    assert rc == 0, "missing manifest must still exit 0"
    assert captured_out.getvalue() == "", "missing manifest must emit nothing"
