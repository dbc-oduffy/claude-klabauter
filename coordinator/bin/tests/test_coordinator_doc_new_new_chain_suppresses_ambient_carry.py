"""`--new-chain` suppresses the AMBIENT carry rungs, and contradictions refuse.

Measured 2026-09-11 on example-store-repo: a handoff scaffolded with `--new-chain`,
no plan and no predecessor, came out carrying the deliverable_id of the plan
the session held a claim on -- work with nothing to do with the new baton. The
`plan` and `sizing-object` arms already honoured the flag through
`_resolve_session_chain_deliverable_id`; the `handoff` arm consulted no such
switch and read the claimed plan unconditionally.

Why it is worse than a wrong field: the deliverable_id is the spine key. Two
unrelated works sharing one read as a single chain and the LoE rollup sums
across both, and the output says "carry path", which reads like carrying rather
than like inventing a link.

Both verdicts are covered: the ambient rungs stay live without the flag.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CLI = Path(__file__).resolve().parents[1] / "coordinator-doc-new.py"


def _load():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_uc", _CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cli():
    return _load()


def test_new_chain_with_an_explicit_deliverable_id_is_refused(cli, capsys, tmp_path):
    rc = cli.main([
        "--type", "handoff",
        "--title", "A baton that names two chains at once",
        "--new-chain",
        "--deliverable-id", "dlv-somewhere-else-abc123",
        "--out", str(tmp_path / "out.md"),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--new-chain" in err and "--deliverable-id" in err
    assert not (tmp_path / "out.md").exists()


def test_new_chain_with_a_predecessor_is_refused(cli, capsys, tmp_path):
    rc = cli.main([
        "--type", "handoff",
        "--title", "A baton that roots and descends at once",
        "--new-chain",
        "--predecessor", "state/handoffs/whatever.md",
        "--out", str(tmp_path / "out.md"),
    ])
    assert rc == 1
    assert "--predecessor" in capsys.readouterr().err
    assert not (tmp_path / "out.md").exists()


def test_the_env_rung_is_live_without_the_flag_and_suppressed_with_it(
    cli, tmp_path, monkeypatch
):
    """DELIVERABLE_ID is whatever the session last exported -- an earlier
    baton_assemble directive, or a peer's. It is the mechanism the skill layer
    uses to propagate a real parent, so it stays live by default; an author who
    has just declared this artifact a chain ROOT has said it does not apply."""
    monkeypatch.setenv("DELIVERABLE_ID", "dlv-ambient-from-the-session-abc123")

    carried = tmp_path / "carried.md"
    assert cli.main([
        "--type", "handoff",
        "--title", "A baton inheriting the session chain",
        "--out", str(carried),
    ]) == 0
    assert "dlv-ambient-from-the-session-abc123" in carried.read_text(encoding="utf-8")

    rooted = tmp_path / "rooted.md"
    assert cli.main([
        "--type", "handoff",
        "--title", "A baton rooting its own chain",
        "--new-chain",
        "--out", str(rooted),
    ]) == 0
    assert "dlv-ambient-from-the-session-abc123" not in rooted.read_text(encoding="utf-8")
