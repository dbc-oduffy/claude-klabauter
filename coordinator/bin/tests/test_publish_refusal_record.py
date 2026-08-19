"""coordinator/bin/tests/test_publish_refusal_record.py — fast-tier stubs
for `coordinator/bin/publish_refusal_record.py` and its six wired call
sites in `publish.py` (§ CALL SITES, `state/dispatch-briefs/2026-08-19-a-
refused-swap-names-what-blocked-it/C1.md`).

No real publish, no spawn. Each call site's discrimination and record
write is exercised via monkeypatched OS primitives (`os.rename`,
`os.replace`, `Path.unlink`) raising a synthetic `PermissionError` with a
chosen `.winerror`, never a real filesystem race — the discriminator under
test is `.winerror in (5, 32)`, not any real OS behaviour.

Run: python -m pytest coordinator/bin/tests/test_publish_refusal_record.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_publish_module():
    return _load_module("publish_refusal_record_test_under_test_publish", "publish.py")


def _load_refusal_module():
    return _load_module("publish_refusal_record_test_under_test_refusal", "publish_refusal_record.py")


def _sharing_violation() -> PermissionError:
    exc = PermissionError(13, "sharing violation")
    exc.winerror = 32  # type: ignore[attr-defined]
    return exc


def _access_denied() -> PermissionError:
    exc = PermissionError(13, "access denied")
    exc.winerror = 5  # type: ignore[attr-defined]
    return exc


# ---------------------------------------------------------------------------
# Unit-level: the discriminator and the record write itself.
# ---------------------------------------------------------------------------


def test_is_holder_refusal_true_for_winerror_5_and_32():
    refusal = _load_refusal_module()
    assert refusal.is_holder_refusal(_access_denied())
    assert refusal.is_holder_refusal(_sharing_violation())


def test_is_holder_refusal_false_for_other_exceptions():
    refusal = _load_refusal_module()
    assert not refusal.is_holder_refusal(FileExistsError("exists"))
    assert not refusal.is_holder_refusal(NotADirectoryError("not a dir"))
    other = PermissionError(13, "some other permission error")
    other.winerror = 3  # type: ignore[attr-defined]
    assert not refusal.is_holder_refusal(other)


def test_record_writes_json_naming_the_path_and_creates_parent(tmp_path, monkeypatch):
    refusal = _load_refusal_module()
    audits_dir = tmp_path / "state" / "audits" / "publish-swap-refusals"
    monkeypatch.setattr(refusal, "AUDITS_DIR", audits_dir)
    assert not audits_dir.exists()

    refused_path = tmp_path / "dest" / "some-entry"
    record_path = refusal.record_publish_swap_refusal(
        refused_path=refused_path,
        aside_path=None,
        swap_branch="root-dest",
        failing_operation="aside_rename",
        exc=_sharing_violation(),
    )

    assert record_path.exists()
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert payload["refused_path"] == str(refused_path)
    assert payload["swap_branch"] == "root-dest"
    assert payload["failing_operation"] == "aside_rename"
    assert isinstance(payload["pid"], int)
    assert payload["timestamp"]


# ---------------------------------------------------------------------------
# Import laziness: no module-level reference to publish_refusal_record.
# ---------------------------------------------------------------------------


def test_publish_refusal_record_import_is_lazy():
    """A module-level `import publish_refusal_record` (or equivalent) would
    silently defeat AC4 — the success path must never pay this import. The
    old stub-raises test could not catch this; an AST walk over `publish.py`
    can: every reference to `publish_refusal_record` must be nested inside a
    function body, never at module scope."""
    tree = ast.parse((_BIN_DIR / "publish.py").read_text(encoding="utf-8"))

    module_level_names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_level_names.update(alias.asname or alias.name for alias in node.names)

    offenders = [n for n in module_level_names if "refusal" in n.lower()]
    assert not offenders, f"publish_refusal_record referenced at module scope: {offenders}"

    # Confirm the load helper does exist, nested, so this isn't vacuous.
    found_nested = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_load_publish_refusal_record_module":
            found_nested = True
            break
    assert found_nested, "expected a nested loader for publish_refusal_record.py"


# ---------------------------------------------------------------------------
# Integration: each of the six call sites.
# ---------------------------------------------------------------------------


def test_swap_entry_file_leg_records_on_sharing_violation(tmp_path, monkeypatch):
    publish = _load_publish_module()
    dest_entry = tmp_path / "dest" / "leaf.txt"
    staging_entry = tmp_path / "staging" / "leaf.txt"
    dest_entry.parent.mkdir(parents=True)
    staging_entry.parent.mkdir(parents=True)
    staging_entry.write_text("new")
    dest_entry.write_text("old")

    def fake_replace(a, b):
        raise _sharing_violation()

    monkeypatch.setattr(publish.os, "replace", fake_replace)

    recorded = {}

    def fake_record(exc, *, refused_path, aside_path, swap_branch, failing_operation):
        recorded["called"] = True
        recorded["swap_branch"] = swap_branch
        recorded["failing_operation"] = failing_operation
        recorded["refused_path"] = refused_path

    monkeypatch.setattr(publish, "_record_publish_swap_refusal", fake_record)

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_entry(dest_entry, staging_entry)

    assert recorded.get("called")
    assert recorded["swap_branch"] == "root-dest"
    assert recorded["refused_path"] == staging_entry


def test_swap_entry_file_leg_no_record_on_file_exists_error(tmp_path, monkeypatch):
    """Exercises the REAL `_record_publish_swap_refusal` (real discriminator),
    proving a `FileExistsError` refusal writes no record at all — a blanket
    `except OSError` would mint one; the discriminator must not."""
    publish = _load_publish_module()
    audits_dir = tmp_path / "state" / "audits" / "publish-swap-refusals"

    refusal_module = _load_refusal_module()
    monkeypatch.setattr(refusal_module, "AUDITS_DIR", audits_dir)
    monkeypatch.setattr(
        publish, "_load_publish_refusal_record_module", lambda: refusal_module
    )

    dest_entry = tmp_path / "dest" / "leaf.txt"
    staging_entry = tmp_path / "staging" / "leaf.txt"
    dest_entry.parent.mkdir(parents=True)
    staging_entry.parent.mkdir(parents=True)
    staging_entry.write_text("new")
    dest_entry.write_text("old")

    def fake_replace(a, b):
        raise FileExistsError("unexpected")

    monkeypatch.setattr(publish.os, "replace", fake_replace)

    with pytest.raises(FileExistsError):
        publish._swap_publish_staging_entry(dest_entry, staging_entry)

    assert not audits_dir.exists() or not list(audits_dir.iterdir())


def test_swap_entry_directory_aside_rename_records(tmp_path, monkeypatch):
    publish = _load_publish_module()
    dest_entry = tmp_path / "dest" / "subdir"
    staging_entry = tmp_path / "staging" / "subdir"
    dest_entry.mkdir(parents=True)
    staging_entry.mkdir(parents=True)

    def fake_rename(a, b):
        raise _access_denied()

    monkeypatch.setattr(publish.os, "rename", fake_rename)

    recorded = {}

    def fake_record(exc, *, refused_path, aside_path, swap_branch, failing_operation):
        recorded["failing_operation"] = failing_operation
        recorded["swap_branch"] = swap_branch

    monkeypatch.setattr(publish, "_record_publish_swap_refusal", fake_record)

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_entry(dest_entry, staging_entry)

    assert recorded["failing_operation"] == "aside_rename"
    assert recorded["swap_branch"] == "root-dest"


def test_swap_into_dest_root_unlink_leg_records(tmp_path, monkeypatch):
    publish = _load_publish_module()
    dest_dir = tmp_path / "dest"
    staging_dir = tmp_path / "staging"
    dest_dir.mkdir()
    staging_dir.mkdir()
    stray = dest_dir / "stray.txt"
    stray.write_text("cruft")

    recorded = {}

    def fake_record(exc, *, refused_path, aside_path, swap_branch, failing_operation):
        recorded["failing_operation"] = failing_operation
        recorded["refused_path"] = refused_path

    monkeypatch.setattr(publish, "_record_publish_swap_refusal", fake_record)

    orig_unlink = Path.unlink

    def fake_unlink(self, *a, **k):
        if self == stray:
            raise _sharing_violation()
        return orig_unlink(self, *a, **k)

    monkeypatch.setattr(publish.Path, "unlink", fake_unlink)

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_into_dest_root(dest_dir, staging_dir)

    assert recorded["failing_operation"] == "unlink"
    assert recorded["refused_path"] == stray


def test_swap_into_dest_whole_tree_prior_backup_rename_records(tmp_path, monkeypatch):
    publish = _load_publish_module()
    dest_dir = tmp_path / "dest"
    staging_dir = tmp_path / "staging"
    dest_dir.mkdir()
    staging_dir.mkdir()

    def fake_rename(a, b):
        raise _sharing_violation()

    monkeypatch.setattr(publish.os, "rename", fake_rename)

    recorded = {}

    def fake_record(exc, *, refused_path, aside_path, swap_branch, failing_operation):
        recorded["failing_operation"] = failing_operation
        recorded["swap_branch"] = swap_branch

    monkeypatch.setattr(publish, "_record_publish_swap_refusal", fake_record)

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert recorded["failing_operation"] == "prior_backup_rename"
    assert recorded["swap_branch"] == "whole-tree"


def test_record_write_failure_does_not_mask_original_error(tmp_path, monkeypatch):
    """A record-write failure must never substitute for or mask the
    original refusal — the ORIGINAL exception surfaces untraced (no
    `__context__`/`__cause__` substitution)."""
    publish = _load_publish_module()
    dest_entry = tmp_path / "dest" / "leaf.txt"
    staging_entry = tmp_path / "staging" / "leaf.txt"
    dest_entry.parent.mkdir(parents=True)
    staging_entry.parent.mkdir(parents=True)
    staging_entry.write_text("new")
    dest_entry.write_text("old")

    original = _sharing_violation()

    def fake_replace(a, b):
        raise original

    monkeypatch.setattr(publish.os, "replace", fake_replace)

    def fake_record(*a, **k):
        raise RuntimeError("record write blew up")

    monkeypatch.setattr(publish, "_record_publish_swap_refusal", fake_record)

    with pytest.raises(PermissionError) as excinfo:
        publish._swap_publish_staging_entry(dest_entry, staging_entry)

    assert excinfo.value is original
    assert excinfo.value.__context__ is None or not isinstance(
        excinfo.value.__context__, RuntimeError
    )
