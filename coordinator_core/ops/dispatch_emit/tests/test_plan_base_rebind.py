"""dispatch.rebind_plan_base: baseline_ref and register pins rebound in place to a new stamp."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import plan_base_rebind as op
from coordinator_core.ops.dispatch_emit.rebind_contract import APPLIED, PINS, STAMP
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/p.md"
SIZING = "state/sizings/s.yaml"
OLD = "a" * 40

PLAN_TEXT = f"""---
title: p  # keep this comment
register_claims:
  sizing: "{SIZING}"
  rows: ["R1"]
  pins: {{"R1": "sha256:{'0' * 64}"}}
prime_exit_criterion:
  falsifier:
    how: "pytest"
    baseline_ref: {OLD}  # base comment
grouping_approvals:
  do:
    status: pending  # load-bearing comment
---

# body
"""

SIZING_TEXT = """requirement_register:
  rows:
    - id: R1
      source_text: "ship the thing"
other: 1
"""


def _git(repo, *args):
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return done.stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    first = _commit(tmp_path, "one")
    second = _commit(tmp_path, "two")
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / PLAN).write_text(PLAN_TEXT, encoding="utf-8", newline="")
    (tmp_path / SIZING).write_text(SIZING_TEXT, encoding="utf-8")
    return tmp_path, first, second


def _call(root, **params):
    return op._handler(params, root)


def _plan(root):
    return (root / PLAN).read_text(encoding="utf-8")


def test_default_stamp_is_head_and_pins_recompute(repo):
    root, _, head = repo
    out = _call(root, plan=PLAN)
    expected = rr.row_pin("ship the thing")
    assert out == {"ok": True, "plan": PLAN, APPLIED: True, STAMP: head, PINS: {"R1": expected}}
    text = _plan(root)
    assert f"baseline_ref: {head}  # base comment" in text
    assert expected in text and "0" * 64 not in text


def test_only_the_two_lines_change(repo):
    root, *_ = repo
    _call(root, plan=PLAN)
    old, new = PLAN_TEXT.splitlines(), _plan(root).splitlines()
    assert len(old) == len(new)
    assert [i for i, (a, b) in enumerate(zip(old, new)) if a != b] == [
        i for i, line in enumerate(old) if "pins:" in line or "baseline_ref:" in line
    ]
    assert "# keep this comment" in new[1] and "# load-bearing comment" in _plan(root)


def test_second_call_is_idempotent(repo):
    root, *_ = repo
    _call(root, plan=PLAN)
    before = (root / PLAN).read_bytes()
    out = _call(root, plan=PLAN)
    assert out["ok"] and out[APPLIED] is False
    assert (root / PLAN).read_bytes() == before


def test_explicit_ancestor_stamp_binds(repo):
    root, first, _ = repo
    out = _call(root, plan=PLAN, stamp=first)
    assert out["ok"] and out[STAMP] == first
    assert f"baseline_ref: {first}" in _plan(root)


def test_stamp_not_an_ancestor_refuses_and_writes_nothing(repo):
    root, *_ = repo
    _git(root, "checkout", "-q", "-b", "side", "HEAD~1")
    foreign = _commit(root, "side")
    _git(root, "checkout", "-q", "-")
    before = (root / PLAN).read_bytes()
    out = _call(root, plan=PLAN, stamp=foreign)
    assert not out["ok"] and "not an ancestor" in out["error"]
    assert (root / PLAN).read_bytes() == before


@pytest.mark.parametrize("stamp", ["abc123", "z" * 40, "f" * 40])
def test_malformed_or_unknown_stamp_refuses(repo, stamp):
    root, *_ = repo
    out = _call(root, plan=PLAN, stamp=stamp)
    assert not out["ok"]
    assert f"baseline_ref: {OLD}" in _plan(root)


def test_claimed_row_absent_from_register_refuses_named(repo):
    root, *_ = repo
    (root / SIZING).write_text(SIZING_TEXT.replace("R1", "R9"), encoding="utf-8")
    before = (root / PLAN).read_bytes()
    out = _call(root, plan=PLAN)
    assert not out["ok"] and "R1" in out["error"]
    assert (root / PLAN).read_bytes() == before


def test_plan_without_baseline_ref_refuses(repo):
    root, *_ = repo
    (root / PLAN).write_text(PLAN_TEXT.replace(f"    baseline_ref: {OLD}  # base comment\n", ""), encoding="utf-8")
    out = _call(root, plan=PLAN)
    assert not out["ok"] and "baseline_ref" in out["error"]


def test_plan_without_register_claims_rebinds_base_only(repo):
    root, _, head = repo
    bare = PLAN_TEXT.split("register_claims:")[0] + "prime_exit_criterion:" + PLAN_TEXT.split("prime_exit_criterion:")[1]
    (root / PLAN).write_text(bare, encoding="utf-8")
    out = _call(root, plan=PLAN)
    assert out["ok"] and out[APPLIED] and out[PINS] == {} and out[STAMP] == head


def test_plan_outside_repo_refuses(repo):
    root, *_ = repo
    assert not _call(root, plan="../outside.md")["ok"]
    assert not _call(root)["ok"]


def test_crlf_plan_keeps_line_endings(repo):
    root, _, head = repo
    (root / PLAN).write_bytes(PLAN_TEXT.replace("\n", "\r\n").encode("utf-8"))
    out = _call(root, plan=PLAN)
    assert out["ok"] and out[APPLIED]
    raw = (root / PLAN).read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert f"baseline_ref: {head}".encode() in raw
