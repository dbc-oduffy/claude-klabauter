"""
Op tests for `commit_ledger.join_divergence_report`: one git spawn per dispatch,
no gate-shaped payload keys, an honest empty-ledger result, `n` clamping.

`run_git` is replaced by a counting fake returning a canned trailer stream, so
no git process and no real repository is involved. The ledger and handoff
frontmatter are `tmp_path` fixtures.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import coordinator_core.ops.commit_join_divergence_report as mod

pytestmark = pytest.mark.cadence

SHA_AGREE = "a" * 40
SHA_DISAGREE = "b" * 40
SHA_LEDGER_ONLY = "c" * 40
SHA_TRAILER_ONLY = "d" * 40
GATE_KEYS = ("exit_code", "ok", "refused", "verdict")


class _CountingGit:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list = []

    def __call__(self, args, cwd=None, **kwargs):
        self.calls.append((list(args), cwd))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def _stream(rows) -> str:
    return "".join(f"{sha}\x1f{value}\n\x1e\n" for sha, value in rows)


def _write_ledger(ldir: Path, handoff_id: str, shas) -> None:
    ldir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"sha": s}) for s in shas] + ["{not json"]
    (ldir / f"{handoff_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _handoffs(mapping):
    return ([{"handoff_id": h, "deliverable_id": d} for h, d in mapping.items()], [])


def _run(params, git, ldir, handoffs=None, root=None):
    handoffs = handoffs if handoffs is not None else _handoffs({})
    with mock.patch.object(mod, "run_git", git), \
            mock.patch.object(mod.store, "ledger_dir", return_value=ldir), \
            mock.patch.object(mod, "_collect_all_handoffs_for_gate_index", return_value=handoffs), \
            mock.patch.object(mod, "main_worktree_root", return_value=root or Path(".")):
        return asyncio.run(mod._handler(params))


def _fixture(tmp_path: Path):
    ldir = tmp_path / "ledger"
    _write_ledger(ldir, "h-agree", [SHA_AGREE])
    _write_ledger(ldir, "h-disagree", [SHA_DISAGREE])
    _write_ledger(ldir, "h-ledger-only", [SHA_LEDGER_ONLY])
    handoffs = _handoffs(
        {"h-agree": "dlv-one", "h-disagree": "dlv-two", "h-ledger-only": "dlv-three"}
    )
    stream = _stream(
        [
            (SHA_AGREE, "dlv-one"),
            (SHA_DISAGREE, "dlv-other"),
            (SHA_LEDGER_ONLY, ""),
            (SHA_TRAILER_ONLY, "dlv-four"),
        ]
    )
    return ldir, handoffs, stream


def test_one_bucket_per_outcome(tmp_path):
    ldir, handoffs, stream = _fixture(tmp_path)
    out = _run({"n": 50}, _CountingGit(stream), ldir, handoffs, tmp_path)
    assert out["counts"]["agree"] == 1
    assert out["counts"]["disagree"] == 1
    assert out["counts"]["ledger_only"] == 1
    assert out["counts"]["trailer_only"] == 1
    assert out["ledger_files_seen"] == 3
    assert out["commits_examined"] == 4
    assert len(out["disagreements"]) == 1


@pytest.mark.parametrize("n", [1, 7, 200, 1000])
def test_exactly_one_git_call_for_any_n(tmp_path, n):
    ldir, handoffs, stream = _fixture(tmp_path)
    git = _CountingGit(stream)
    _run({"n": n}, git, ldir, handoffs, tmp_path)
    assert len(git.calls) == 1
    assert git.calls[0][0][0] == "log"


def test_no_ledger_is_a_normal_result(tmp_path):
    git = _CountingGit(_stream([(SHA_TRAILER_ONLY, "dlv-four")]))
    out = _run({}, git, None, None, tmp_path)
    assert out["ledger_files_seen"] == 0
    assert "0 files" in out["basis"]
    assert out["counts"]["trailer_only"] == 1
    assert out["counts"]["agree"] == 0
    assert out["counts"]["disagree"] == 0
    assert len(git.calls) == 1


def test_payload_has_no_gate_shape(tmp_path):
    ldir, handoffs, stream = _fixture(tmp_path)
    for lane in (_run({}, _CountingGit(stream), ldir, handoffs, tmp_path),
                 _run({}, _CountingGit(stream), None, None, tmp_path)):
        for key in GATE_KEYS:
            assert key not in lane


@pytest.mark.parametrize(
    "raw,expected", [(0, 1), (-5, 1), (1, 1), (999, 999), (1000, 1000), (5000, 1000)]
)
def test_n_is_clamped(tmp_path, raw, expected):
    git = _CountingGit(_stream([]))
    _run({"n": raw}, git, None, None, tmp_path)
    assert git.calls[0][0][1] == f"-{expected}"
