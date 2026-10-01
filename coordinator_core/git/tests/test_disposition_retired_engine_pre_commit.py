"""Banner parity and exactness for the `retired-claude-klabauter-pre-commit` entry."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.git import hook_dispositions as hd
from coordinator_core.git.hook_disposition_entries import retired_engine_pre_commit as entry_mod

REMOVER = Path(__file__).resolve().parents[3] / "coordinator/bin/remove-claude-klabauter-precommit-hook.py"
RETIRED_BODY = (
    "#!/bin/sh\n"
    "# Registry-driven (coordinator_core.ops.install_claude_klabauter_precommit_hook);\n"
    "# every gate below fails LOUD.\n"
    "exit 0\n"
)


@pytest.fixture
def clone(tmp_path):
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    return tmp_path, hooks


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setattr(hd, "DISPOSITIONS", (entry_mod.ENTRY,), raising=False)


def test_banner_equals_standalone_remover_literal():
    text = REMOVER.read_text(encoding="utf-8")
    found = re.search(r'^_BANNER = "([^"\n]*)"$', text, re.MULTILINE)
    assert found is not None
    assert entry_mod._BANNER == found.group(1)


def test_entry_shape():
    assert entry_mod.ENTRY.id == "retired-claude-klabauter-pre-commit"
    assert entry_mod.ENTRY.hook_name == "pre-commit"
    assert entry_mod.ENTRY.action == "remove"


def test_bannered_hook_is_removed_with_backup(clone, table):
    root, hooks = clone
    (hooks / "pre-commit").write_bytes(RETIRED_BODY.encode())
    assert hd.apply_repo(root, check_only=False) == [
        ("retired-claude-klabauter-pre-commit", "pre-commit", "removed")
    ]
    assert not (hooks / "pre-commit").exists()
    assert (hooks / "pre-commit.retired").read_bytes() == RETIRED_BODY.encode()


def test_check_only_reports_stale_and_leaves_bytes(clone, table):
    root, hooks = clone
    (hooks / "pre-commit").write_bytes(RETIRED_BODY.encode())
    assert hd.classify_repo(root)[0][2] == "stale"
    assert (hooks / "pre-commit").read_bytes() == RETIRED_BODY.encode()


def test_foreign_hook_left_alone(clone, table):
    root, hooks = clone
    foreign = b"#!/bin/sh\necho mine\n"
    (hooks / "pre-commit").write_bytes(foreign)
    assert hd.apply_repo(root, check_only=False)[0][2] == "current"
    assert (hooks / "pre-commit").read_bytes() == foreign


def test_absent_hook(clone, table):
    root, _ = clone
    assert hd.apply_repo(root, check_only=False)[0][2] == "absent"


def test_content_root_installer_hook_left_alone(clone, table):
    from coordinator_core.ops import install_content_root_precommit_hook as doe

    root, hooks = clone
    body = doe._hook_body(doe._GATE_REGISTRY).encode()
    (hooks / "pre-commit").write_bytes(body)
    assert hd.apply_repo(root, check_only=False)[0][2] == "current"
    assert (hooks / "pre-commit").read_bytes() == body


def test_meta_repo_installer_hook_left_alone(clone, table):
    from coordinator_core.ops import install_meta_repo_precommit_hook as meta

    root, hooks = clone
    body = meta._hook_body(root / "bin", meta._GATE_REGISTRY).encode()
    (hooks / "pre-commit").write_bytes(body)
    assert hd.apply_repo(root, check_only=False)[0][2] == "current"
    assert (hooks / "pre-commit").read_bytes() == body
