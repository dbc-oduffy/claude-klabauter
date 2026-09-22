"""Regression guard: the warm-tests short-runtime-base fixtures must not
hardcode POSIX `dir="/tmp"` unconditionally.

Purpose:
state/bug-backlog/2026-09-13-warm-tests-fixtures-hardcode-dir-tmp-bre-1278f1e3e384.yaml
-- `tempfile.mkdtemp(prefix="wrb-", dir="/tmp")` raised
`FileNotFoundError: [WinError 3]` at fixture setup on Windows, because
`/tmp` does not exist there as a drive-relative root, failing every test
that depends on the fixture before its body ever ran.

AST-based, not regex, per the in-repo precedent this module is modelled on
(`coordinator_core/tests/test_no_global_os_name_monkeypatch.py`) -- a regex
over source text cannot reliably distinguish a guarded
`dir=None if os.name == "nt" else "/tmp"` conditional from the bare
hardcode it replaced.

Does NOT monkeypatch the process-global `os.name` to simulate the Windows
branch directly -- `test_no_global_os_name_monkeypatch.py`'s ratchet
forbids any new test-file site doing that (flipping it corrupts every
`pathlib.Path` built for the rest of the process). Reading the fixture's
own source is a platform-independent way to pin the same fact this bug
was reported against: no code path in these fixtures reaches
`tempfile.mkdtemp(dir="/tmp")` unconditionally.

Negative-spec:
    - Does NOT check `test_client_posix_transport.py`'s
      `test_open_pipe_round_trips_over_a_real_unix_socket` body, which
      keeps its own literal `dir="/tmp"` -- that test is decorated
      `@pytest.mark.skipif(sys.platform == "win32", ...)` and never
      reaches setup on Windows, so it is not an instance of this defect.
"""

from __future__ import annotations

import ast
from pathlib import Path

_WARM_TESTS_DIR = Path(__file__).resolve().parent

#: (filename, fixture-function-name) pairs whose `tempfile.mkdtemp` call
#: this bug's fix touched -- every autouse or requested short-runtime-base
#: fixture the bug row's 96-failure count and triage `fix_files` named.
_TARGETS = [
    ("test_client_fallback.py", "_short_warm_runtime_base"),
    ("test_warm_telemetry.py", "_short_warm_runtime_base"),
    ("test_settings_home_mismatch_refusal.py", "_short_warm_runtime_base"),
    ("test_client_posix_transport.py", "_short_warm_runtime_base"),
    ("test_server_starts_http_listener.py", "_short_warm_runtime_base"),
    ("test_credential_directory_is_hardened.py", "runtime_base"),
]


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _mkdtemp_dir_kwargs(func: ast.FunctionDef) -> list[ast.expr]:
    kwargs: list[ast.expr] = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mkdtemp"
        ):
            for kw in node.keywords:
                if kw.arg == "dir":
                    kwargs.append(kw.value)
    return kwargs


def test_short_runtime_base_fixtures_do_not_hardcode_posix_tmp() -> None:
    """Every listed fixture's `mkdtemp(dir=...)` must not be a bare
    `"/tmp"` constant -- on Windows that path does not exist and setup
    raises `WinError 3` before any test body runs."""
    offenders: list[str] = []
    for filename, func_name in _TARGETS:
        path = _WARM_TESTS_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        func = _find_function(tree, func_name)
        dir_kwargs = _mkdtemp_dir_kwargs(func)
        assert dir_kwargs, f"{filename}::{func_name} has no mkdtemp(dir=...) call to check"
        for value in dir_kwargs:
            if isinstance(value, ast.Constant) and value.value == "/tmp":
                offenders.append(f"{filename}::{func_name}")

    assert not offenders, (
        'hardcoded POSIX dir="/tmp" in warm-tests fixture(s), unguarded for '
        "Windows: " + ", ".join(offenders)
    )
