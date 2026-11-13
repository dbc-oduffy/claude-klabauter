"""plan.readiness_verdict base ref, end to end through the registered handler: a premise true at a
ref but absent from the tree reads RESOLVES under a `base_ref` param or a `workflow.revise` run
marker, reads the tree (CONTRADICTED) with neither, and costs one cat-file spawn for N anchors.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.git import run as git_run
from coordinator_core.ops import plan_readiness_verdict as op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REL = "docs/plans/2026-10-29-fixture.md"
SIZING = "state/sizings/2026-10-29-baseref.yaml"
RUN = "ask-20261029T000000-abc123"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _anchor(ident: str, path: str) -> str:
    return (
        f"  - id: {ident}\n    claim: {path} holds the needle\n    check:\n"
        f"      command: grep -c -F needle {path}\n      expect: '1'\n"
    )


@pytest.fixture
def world(tmp_path):
    """A repo whose commit X holds `needle` in a.py and b.py while the tree (HEAD) holds neither."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "a.py").write_text("needle\n", encoding="utf-8")
    (root / "b.py").write_text("needle\n", encoding="utf-8")
    sizing = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}],
    }
    (root / SIZING).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "has needle")
    sha = _git(root, "rev-parse", "HEAD")
    (root / "a.py").write_text("gone\n", encoding="utf-8")
    (root / "b.py").write_text("gone\n", encoding="utf-8")
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-29\n"
        f"sizing_object: {SIZING}\n"
        "census:\n  - question: q\n    command: grep -c a b\n    result: '1'\n"
        "capabilities: []\nanchor_premises:\n" + _anchor("AP1", "a.py") + _anchor("AP2", "b.py") +
        "prime_exit_criterion:\n  statement: the op reports per class\n"
        f"  derived_from: {SIZING}\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "drops needle")
    return root, sha


def _call(root: Path, monkeypatch, **extra) -> tuple:
    """(reply, cat-file spawn count) from the handler at default arguments plus `extra`."""
    calls: list = []
    real = git_run.run_git

    def counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", counting)
    reply = op._handler({"plan": REL, **extra}, root / ".git")
    return reply, sum(1 for a in calls if "cat-file" in a)


def _verdicts(reply: dict) -> dict:
    return {v["id"]: v for v in reply["anchor_verdicts"]}


def test_base_ref_param_resolves_an_anchor_absent_from_the_tree(world, monkeypatch):
    root, sha = world
    reply, _ = _call(root, monkeypatch, base_ref=sha)
    verdicts = _verdicts(reply)
    assert verdicts["AP1"]["verdict"] == "RESOLVES" and verdicts["AP1"]["source"] == "engine-check"
    assert reply["anchor_base_ref"] == {"ref": sha, "source": "param"}
    import json

    import jsonschema

    schema = Path(op.__file__).resolve().parents[1] / "contract" / "readiness-verdict.schema.json"
    jsonschema.validate(reply, json.loads(schema.read_text(encoding="utf-8")))


def test_revise_marker_resolves_with_no_param_on_refire(world, monkeypatch):
    root, sha = world
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING, ask_text="a raw ask")
    assert rl._handler({"run_id": RUN, "base_ref": sha}, repo_root=root).get("ok") is True
    reply, _ = _call(root, monkeypatch)
    assert _verdicts(reply)["AP1"]["verdict"] == "RESOLVES"
    assert reply["anchor_base_ref"] == {"ref": sha, "source": "run"}


def test_param_outranks_the_run_marker(world, monkeypatch):
    root, sha = world
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING, ask_text="a raw ask")
    rl._handler({"run_id": RUN, "base_ref": sha}, repo_root=root)
    reply, _ = _call(root, monkeypatch, base_ref="HEAD")
    assert reply["anchor_base_ref"] == {"ref": "HEAD", "source": "param"}
    assert _verdicts(reply)["AP1"]["verdict"] == "CONTRADICTED"


def test_no_base_ref_reads_the_tree_with_no_cat_file_spawn(world, monkeypatch):
    root, _ = world
    reply, spawns = _call(root, monkeypatch)
    assert _verdicts(reply)["AP1"]["verdict"] == "CONTRADICTED"
    assert "anchor_base_ref" not in reply
    assert spawns == 0


def test_all_anchors_cost_one_cat_file_spawn_under_a_base_ref(world, monkeypatch):
    root, sha = world
    reply, spawns = _call(root, monkeypatch, base_ref=sha)
    assert {v["verdict"] for v in _verdicts(reply).values()} == {"RESOLVES"}
    assert spawns == 1


def test_an_unreadable_base_ref_is_not_a_tree_fallback(world, monkeypatch):
    root, _ = world
    reply, _ = _call(root, monkeypatch, base_ref="no-such-ref")
    verdict = _verdicts(reply)["AP1"]
    assert verdict["verdict"] != "RESOLVES" and "no-such-ref" in verdict["observed"]


@pytest.mark.parametrize("bad", ["", "   ", 7, ["HEAD"]])
def test_a_bad_base_ref_param_is_refused(world, bad):
    root, _ = world
    with pytest.raises(ValueError, match="base_ref"):
        op._handler({"plan": REL, "base_ref": bad}, root / ".git")
