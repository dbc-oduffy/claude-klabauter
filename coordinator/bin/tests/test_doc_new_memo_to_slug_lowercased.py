from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("coordinator_doc_new", str(_BIN_DIR / "coordinator-doc-new.py"))
    spec = importlib.util.spec_from_loader("coordinator_doc_new", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    # main() writes its scaffold relative to cwd; never let it land in the real tree.
    monkeypatch.chdir(tmp_path)


@pytest.fixture()
def doc_new():
    return _load_module()


def test_mixed_case_to_is_lowercased_with_a_notice(doc_new, monkeypatch, capsys):
    # DoE-claude-em is mixed-case per the receiver's own fleet identity; the
    # CLI used to hard-refuse this with "not a valid slug" (memo friction
    # item 1 — cross-repo/inbox/2026-09-28-example-retrieval-repo-em-memo-send-friction.md).
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coordinator-doc-new.py",
            "--type",
            "memo",
            "--to",
            "DoE-claude-em",
            "--topic",
            "a-topic",
            "--kind",
            "fyi",
        ],
    )

    try:
        doc_new.main()
    except SystemExit:
        pass

    stderr = capsys.readouterr().err
    assert "not a valid slug" not in stderr
    assert "'DoE-claude-em' lowercased to 'doe-claude-em'" in stderr


def test_all_lowercase_to_is_unaffected(doc_new, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coordinator-doc-new.py",
            "--type",
            "memo",
            "--to",
            "doe-claude-em",
            "--topic",
            "a-topic",
            "--kind",
            "fyi",
        ],
    )

    try:
        doc_new.main()
    except SystemExit:
        pass

    stderr = capsys.readouterr().err
    assert "not a valid slug" not in stderr
    assert "lowercased" not in stderr


def test_genuinely_invalid_to_still_refuses(doc_new, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coordinator-doc-new.py",
            "--type",
            "memo",
            "--to",
            "em!!",
            "--topic",
            "a-topic",
            "--kind",
            "fyi",
        ],
    )

    try:
        code = doc_new.main()
    except SystemExit as exc:
        code = exc.code

    stderr = capsys.readouterr().err
    assert code == 1
    assert "not a valid slug" in stderr


def test_memo_default_out_path_is_the_canonical_outbox(doc_new):
    # `.coordinator-local/memo-outbox/` is the canonical outbox dir
    # (`coordinator_core.ops.fleet.memo_draft.outbox_dir`'s own
    # `MUTATES`); `state/memo-outbox/` is the retired read-fallback only.
    # Two scaffolders writing two different "canonical" outboxes was memo
    # friction item 6.
    import os

    path = doc_new._default_output_path(
        doc_type="memo",
        title="A title",
        topic="a-topic",
    )
    assert path == os.path.join(".coordinator-local", "memo-outbox", "a-topic.md")
