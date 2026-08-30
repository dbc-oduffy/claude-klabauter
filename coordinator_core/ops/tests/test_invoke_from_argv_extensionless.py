"""coordinator_core.ops.tests.test_invoke_from_argv_extensionless -- tests for
chunk C3 of docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-are-
thoroughly-dead.md: `_resolve_entrypoint_script` learns the extensionless
`coordinator/bin/<name>` fallback, so a door image whose only on-disk script
is extensionless (the twelve) can still resolve.

Coverage:
  (a) `<name>.py` still wins when both `<name>.py` and the extensionless
      `<name>` exist -- the fallback must never override an existing `.py`.
  (b) an allowlisted name with ONLY an extensionless script (no `.py`)
      resolves to that extensionless script.
  (c) an allowlisted name with neither `<name>.py` nor an extensionless
      `<name>` still fails closed with a plain `ValueError`, naming both
      candidate paths.
  (d) a directory at the extensionless path does not count as a script --
      resolution still fails closed rather than treating a directory as a
      loadable module file.

Fixture CLIs are written to a temporary directory and reached by monkey-
patching `_ENGINE_ROOT`/`_WARM_ENTRYPOINT_ALLOWLIST` module globals, matching
test_entrypoint_resolution.py's own convention -- never by writing into the
real `coordinator/bin/`, which is outside this chunk's `writes:` scope.

Spec backlink: docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-
    are-thoroughly-dead.md, chunk C3
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops.invoke_from_argv as ifa
from coordinator_core.ops.invoke_from_argv import _resolve_entrypoint_script


def _bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    return bin_dir


def test_py_suffix_wins_when_both_candidates_exist(monkeypatch, tmp_path):
    bin_dir = _bin_dir(tmp_path)
    (bin_dir / "both-forms.py").write_text("def main(argv):\n    return 0\n", encoding="utf-8")
    (bin_dir / "both-forms").write_text("not a python module\n", encoding="utf-8")

    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"both-forms"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    resolved = _resolve_entrypoint_script("both-forms")
    assert resolved == bin_dir / "both-forms.py"


def test_extensionless_script_resolves_when_no_py_exists(monkeypatch, tmp_path):
    bin_dir = _bin_dir(tmp_path)
    (bin_dir / "static-check").write_text("def main(argv):\n    return 0\n", encoding="utf-8")

    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"static-check"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    resolved = _resolve_entrypoint_script("static-check")
    assert resolved == bin_dir / "static-check"


def test_neither_candidate_present_fails_closed_naming_both_paths(monkeypatch, tmp_path):
    bin_dir = _bin_dir(tmp_path)

    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"ghost-cli"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    with pytest.raises(ValueError) as excinfo:
        _resolve_entrypoint_script("ghost-cli")

    message = str(excinfo.value)
    assert str(bin_dir / "ghost-cli.py") in message
    assert str(bin_dir / "ghost-cli") in message


def test_directory_at_extensionless_path_does_not_resolve(monkeypatch, tmp_path):
    bin_dir = _bin_dir(tmp_path)
    (bin_dir / "dir-only").mkdir()

    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({"dir-only"}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", tmp_path)

    with pytest.raises(ValueError):
        _resolve_entrypoint_script("dir-only")
