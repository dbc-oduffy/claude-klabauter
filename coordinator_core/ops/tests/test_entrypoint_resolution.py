"""coordinator_core.ops.tests.test_entrypoint_resolution -- tests for chunk
C1 of the multi-name native-invocation surface (docs/research/spike-verdicts/
2026-08-27-multi-name-native-invocation-surface.md): generalized entrypoint
resolution over a committed warm-load allowlist, plus op-boundary
containment of a loaded CLI's `SystemExit`/exceptions.

Coverage:
  (a) the committed allowlist (`warm_entrypoint_allowlist.json`) is seeded
      with ONLY C0's proving CLI (`cross-repo-memo`) -- C1 ships the
      mechanism plus an empty-apart-from-that allowlist; C2 populates the
      rest from its door-eligible census.
  (b) resolution and warm-safety are different properties: an entrypoint
      absent from the allowlist fails closed even when its
      `coordinator/bin/<name>.py` script genuinely exists on disk -- proven
      against the REAL `cross-repo-memo` script with the allowlist patched
      empty, so this is not a fixture artifact.
  (c) allowlist membership is necessary but not sufficient: an allowlisted
      name whose script does not exist still fails closed (existing
      `_resolve_entrypoint_script` behaviour, re-asserted under the new
      allowlist-gated resolution path).
  (d) op-boundary containment -- a `coordinator/bin/<name>.py` CLI that is
      ON the allowlist and DOES exist, but whose module body calls
      `sys.exit(N)` at IMPORT time (before `main` is ever reached), never
      propagates a `SystemExit` out of the op: it comes back as an ordinary
      `{"exit_code": N}` result, exactly like a `SystemExit` raised from
      inside `main` already was before this chunk.
  (e) op-boundary containment -- a module body that raises an arbitrary
      exception at import time is CAUGHT, not propagated: the op returns
      `exit_code == 1` and a diagnostic on stderr, rather than killing the
      calling test process (standing in for the shared warm server).
  (f) op-boundary containment -- `main`'s own OLD contract (an exception
      raised from inside `main`, as opposed to at module-import time) is
      still contained the same way, now going through the same single
      `try`/`except` as the module-load step.

Fixture CLIs are written to a temporary directory and reached by monkey-
patching `_ENGINE_ROOT`/`_WARM_ENTRYPOINT_ALLOWLIST` module globals -- never
by writing into the real `coordinator/bin/`, which is outside this chunk's
`writes:` scope.

Spec backlink: docs/research/spike-verdicts/2026-08-27-multi-name-native-
    invocation-surface.md, chunk C1
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.ops.invoke_from_argv as ifa
from coordinator_core.ops.invoke_from_argv import _ALLOWLIST_PATH, _invoke_from_argv

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# (a) committed allowlist seeded with only C0's proving CLI
# ---------------------------------------------------------------------------

def test_allowlist_seeded_with_only_the_proving_cli():
    data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    assert set(data["entrypoints"]) == {"cross-repo-memo"}, (
        "C1 must seed the committed allowlist with ONLY C0's proving CLI -- "
        "everything else arrives via C2's door-eligible census, not this chunk"
    )


def test_committed_allowlist_file_is_valid_json_with_entrypoints_key():
    data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    assert isinstance(data["entrypoints"], list)
    assert all(isinstance(name, str) and name for name in data["entrypoints"])


# ---------------------------------------------------------------------------
# (b) allowlist gates independently of on-disk existence
# ---------------------------------------------------------------------------

def test_entrypoint_not_on_allowlist_fails_closed_even_though_script_exists(monkeypatch):
    real_bin = _PROJECT_ROOT / "coordinator" / "bin" / "cross-repo-memo.py"
    assert real_bin.is_file(), "setup error: real proving CLI must exist"

    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset())

    with pytest.raises(ValueError, match="allowlist") as excinfo:
        _invoke_from_argv({
            "argv": ["list"],
            "cwd": str(_PROJECT_ROOT),
            "entrypoint": "cross-repo-memo",
        })
    assert "cross-repo-memo" in str(excinfo.value)


def test_entrypoint_on_allowlist_resolves_the_real_script(monkeypatch):
    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"cross-repo-memo"}))
    result = _invoke_from_argv({
        "argv": ["list", "--help"],
        "cwd": str(_PROJECT_ROOT),
        "entrypoint": "cross-repo-memo",
    })
    assert result["exit_code"] == 0
    assert "usage" in result["stdout"].lower()


# ---------------------------------------------------------------------------
# (c) allowlist membership is necessary, not sufficient
# ---------------------------------------------------------------------------

def test_allowlisted_name_with_no_script_still_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"ghost-cli"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    with pytest.raises(ValueError) as excinfo:
        _invoke_from_argv({
            "argv": [],
            "cwd": str(_PROJECT_ROOT),
            "entrypoint": "ghost-cli",
        })
    message = str(excinfo.value)
    assert "ghost-cli" in message
    assert "coordinator-invoke" not in message


# ---------------------------------------------------------------------------
# (d)/(e)/(f) op-boundary containment
# ---------------------------------------------------------------------------

def _write_fixture_cli(tmp_path: Path, name: str, body: str) -> None:
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / f"{name}.py").write_text(body, encoding="utf-8")


def test_module_level_sys_exit_is_contained_not_propagated(monkeypatch, tmp_path):
    _write_fixture_cli(
        tmp_path,
        "exits-at-import",
        "import sys\nsys.exit(3)\n\ndef main(argv):\n    return 0\n",
    )
    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"exits-at-import"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    result = _invoke_from_argv({
        "argv": [],
        "cwd": str(_PROJECT_ROOT),
        "entrypoint": "exits-at-import",
    })
    assert result["exit_code"] == 3, (
        "a module-level sys.exit must come back as an ordinary exit_code, "
        "never as a SystemExit propagating out of the op"
    )


def test_module_level_exception_is_contained_not_propagated(monkeypatch, tmp_path):
    _write_fixture_cli(
        tmp_path,
        "raises-at-import",
        "raise RuntimeError('boom at import')\n\ndef main(argv):\n    return 0\n",
    )
    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"raises-at-import"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    result = _invoke_from_argv({
        "argv": [],
        "cwd": str(_PROJECT_ROOT),
        "entrypoint": "raises-at-import",
    })
    assert result["exit_code"] == 1, (
        "an arbitrary exception raised while loading the module must be "
        "caught at the op boundary, never propagate and kill the caller"
    )
    assert "raises-at-import" in result["stderr"]
    assert "boom at import" in result["stderr"]


def test_main_raising_exception_is_still_contained(monkeypatch, tmp_path):
    _write_fixture_cli(
        tmp_path,
        "raises-in-main",
        "def main(argv):\n    raise RuntimeError('boom in main')\n",
    )
    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"raises-in-main"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    result = _invoke_from_argv({
        "argv": [],
        "cwd": str(_PROJECT_ROOT),
        "entrypoint": "raises-in-main",
    })
    assert result["exit_code"] == 1
    assert "boom in main" in result["stderr"]


def test_main_raising_system_exit_still_contained(monkeypatch, tmp_path):
    _write_fixture_cli(
        tmp_path,
        "exits-in-main",
        "def main(argv):\n    raise SystemExit(7)\n",
    )
    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"exits-in-main"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    result = _invoke_from_argv({
        "argv": [],
        "cwd": str(_PROJECT_ROOT),
        "entrypoint": "exits-in-main",
    })
    assert result["exit_code"] == 7
