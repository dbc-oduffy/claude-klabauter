"""parse_prefix_claims: single-line, block and none forms; every copy delegates to it."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import land_run, land_run_halt, terminal_commit_ledger
from coordinator_core.ops.dispatch_emit.commit_request import parse_prefix_claims


def test_block_form_returns_every_path():
    text = "DONE: C1\ncreated-under-prefix:\ndocs/decisions/a.md\n- `docs/decisions/b.md`\n\nNotes: x\n"
    assert parse_prefix_claims(text) == ["docs/decisions/a.md", "docs/decisions/b.md"]


def test_single_line_form_unchanged():
    assert parse_prefix_claims("created-under-prefix: docs/x/a.md\n") == ["docs/x/a.md"]


def test_mixed_single_line_and_block():
    text = "created-under-prefix: docs/x/a.md\ncreated-under-prefix:\ndocs/x/b.md\ndocs/x/c.md\n"
    assert parse_prefix_claims(text) == ["docs/x/a.md", "docs/x/b.md", "docs/x/c.md"]


def test_block_ended_by_next_field():
    text = "created-under-prefix:\ndocs/x/a.md\nNotes: docs/x/not-a-claim.md\ndocs/x/also-not.md\n"
    assert parse_prefix_claims(text) == ["docs/x/a.md"]


def test_block_ended_by_status_line():
    text = "created-under-prefix:\ndocs/x/a.md\nDONE\ndocs/x/not-a-claim.md\n"
    assert parse_prefix_claims(text) == ["docs/x/a.md"]


def test_block_ended_by_blank_line():
    assert parse_prefix_claims("created-under-prefix:\ndocs/x/a.md\n\ndocs/x/no.md\n") == ["docs/x/a.md"]


def test_none_claims_nothing():
    assert parse_prefix_claims("created-under-prefix: none\n") == []
    assert parse_prefix_claims("created-under-prefix:\n\nNotes: x\n") == []


def test_no_label_returns_none():
    assert parse_prefix_claims("DONE: C1\nnothing here\n") is None


def test_ledger_alias_is_the_shared_parser():
    assert terminal_commit_ledger._parse_prefix_claims is parse_prefix_claims


def test_land_run_and_halt_read_block_form(tmp_path):
    report = tmp_path / "r.md"
    report.write_text("DONE\ncreated-under-prefix:\ndocs/d/a.md\ndocs/other/b.md\n", encoding="utf-8")
    chunk = SimpleNamespace(prefixes=("docs/d/",), report="r.md")
    assert land_run._own_prefix_files(tmp_path, chunk, {}) == ["docs/d/a.md"]
    assert land_run_halt._own_prefix_files(tmp_path, chunk, {}) == ["docs/d/a.md"]
    assert terminal_commit_ledger._own_prefix_files(tmp_path, chunk, {}) == ["docs/d/a.md"]
