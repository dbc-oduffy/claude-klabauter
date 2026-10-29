"""A refused dispatch.terminal_commit codes the spine rows a halt commit already landed and releases
their dependents; unlanded rows stay open and a run-token refusal stamps nothing."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import run_token, spine_read, terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    PLAN,
    RUN,
    _git,
    _verdict,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN_TEXT = (
    "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
    + "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n"
        f"  writes:\n    - {i.lower()}.py\n  disposition: open\n  deferred: false\n"
        + ("  depends_on:\n    - chunk: A\n      gate_kind: output-consumption-runtime\n      note: n\n" if i == "B" else "")
        for i in ("A", "B", "C")
    )
    + "```\n"
)


@pytest.fixture
def halted(repo):  # noqa: F811
    """Plan committed, then a halt record commit lands a.py only."""
    _write(repo, PLAN, _PLAN_TEXT)
    _git(repo, "add", PLAN)
    _git(repo, "commit", "-q", "-m", "seed plan")
    _write(repo, "a.py", "work\n")
    _git(repo, "add", "a.py")
    _git(
        repo, "commit", "-q", "-m", "warp: record halted run1 run artifacts",
        "-m", f"Halt-Plan: {PLAN}\nHalted-By: ledger-check\nLands-Chunks: ",
    )
    return repo


def _rows(root) -> dict:
    text = (root / PLAN).read_text(encoding="utf-8")
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block(text).body)}


def _call(root, token=None) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": _verdict({}), "run_id": RUN, "plan_path": PLAN,
    }
    if token is not None:
        params["run_token"] = token
    return terminal_commit._handler(params, repo_root=root / ".git")


def _spine(root) -> dict:
    return {r.id: r for r in spine_read.read_spine(root / PLAN, schema_preflight=False)}


def test_refusal_codes_the_halt_landed_row_and_releases_its_dependent(halted):
    halt = _git(halted, "rev-parse", "HEAD").strip()
    assert list(_spine(halted)["B"].depends_on)

    out = _call(halted)
    assert out["committed"] is False and out["refused"] == "ledger-unmet"
    assert out["refusal_rows_coded"] == {PLAN: ["A"]}, out

    rows = _rows(halted)
    assert rows["A"]["disposition"] == "coded"
    assert rows["A"]["disposition_ref"] == halt
    assert _row_disposition(rows["B"]) == "open"
    assert _row_disposition(rows["C"]) == "open"
    after = _spine(halted)
    assert set(after) == {"B", "C"}
    assert not list(after["B"].depends_on)


def test_a_run_token_in_flight_refusal_stamps_nothing(halted):
    token = run_token.mint(halted, RUN, "run.mjs")
    assert run_token.redeem(halted, RUN, token, "run.mjs")
    out = _call(halted, token)
    assert out["refused"] == "run-token-in-flight"
    assert "refusal_rows_coded" not in out
    assert all(_row_disposition(r) == "open" for r in _rows(halted).values())


def test_a_peer_claim_naming_the_plan_stamps_nothing():
    reply = {"refused": "peer-claim", "peer_claims": [{"path": PLAN, "session": "s"}]}
    assert terminal_commit._stamp_on_refusal({"plan_path": PLAN}, object(), reply) == {}
    reply = {"refused": "blob-drift", "blob_drift": [{"path": PLAN}]}
    assert terminal_commit._stamp_on_refusal({"plan_path": PLAN}, object(), reply) == {}
