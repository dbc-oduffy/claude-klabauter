"""A verify node's brief follows the graph: no fix node reaches it, so it runs before any fix and
judges routing; a fix node reaches it, so it keeps the fix-on-disk brief."""
import re

import yaml

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

from .conftest import REVIEW_KW
from .test_grind_compose import _FIXTURE_PROFILE_DIR, _fixture_manifest

_POST_FIX_MARK = "Try to reject the fix you are handed"
_ROUTING_MARK = "Judge the routing only"


def _routing_profile(tmp_path):
    doc = yaml.safe_load((_FIXTURE_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8"))
    g = doc["graph"]
    g["triage"]["edges"]["confirmed-bug"] = "verify-routing"
    g["verify-routing"] = {"kind": "verify", "verify": {"default": "agent"}, "edges": {"pass": "fix"}}
    (tmp_path / "fixture.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    profile = gp.load_profile("fixture", tmp_path)
    gp.validate_graph(profile)
    return profile


def _script(profile):
    return gc.compose_grind_script(
        _fixture_manifest(), profile, gp.resolve_appetite(profile, "standard"),
        run_dir="state/queue-grind/fixture/run-1", appetite="standard", agent_type_host=None,
        queue_dirs=["state/bug-backlog"], **REVIEW_KW,
    )


def test_only_the_verify_no_fix_reaches_is_pre_fix(tmp_path):
    profile = _routing_profile(tmp_path)
    assert gc.pre_fix_verify_nodes(gc.build_routing_table(profile)) == {"verify-routing"}


def test_fix_before_verify_graph_has_no_pre_fix_verify():
    profile = gp.load_profile("fixture", _FIXTURE_PROFILE_DIR)
    assert gc.pre_fix_verify_nodes(gc.build_routing_table(profile)) == frozenset()


def test_one_verify_prompt_carries_both_briefs_chosen_by_the_node(tmp_path):
    script = _script(_routing_profile(tmp_path))
    assert 'const VERIFY_PRE_FIX = ["verify-routing"];' in script
    call = re.search(r"VERIFY_PRE_FIX\.includes\(row\.node\)\) \? \((.*?)\) : \(", script, re.S).group(1)
    assert _ROUTING_MARK in call and "do not require any change on disk" in call
    assert _POST_FIX_MARK not in call
    assert script.count(_POST_FIX_MARK) == 1


def test_pre_fix_verify_failing_twice_is_handed_back_without_undo():
    routing = {
        "triage": {"kind": "triage", "edges": {"bug": "verify-routing"}, "on_fail": None},
        "verify-routing": {"kind": "verify", "edges": {"pass": "fix"}, "on_fail": None},
        "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": None},
        "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": None},
        "commit": {"kind": "commit", "edges": {}, "on_fail": None},
    }
    assert gc.pre_fix_verify_nodes(routing) == {"verify-routing"}
    calls = []

    def agent(kind, unit):
        calls.append(kind)
        if kind == "triage":
            return [{"row": "r0", "verdict": "bug", "tshirt_size": "S", "tradeoff": ""}]
        if kind == "verify":
            return {"outcome": "fail", "reason": "wrong target"}
        return {}

    class Budget:
        total = None
        _s = 0
        def spent(self): return self._s
        def remaining(self): return None
        def spend(self, n): self._s += n

    result = gc.run_admission(
        [("b0", ["r0"])], routing, "triage", reserve=gc.batch_reserve(1),
        max_agent_calls=None, budget=Budget(), agent=agent,
    )
    assert calls.count("verify") == 2 and "fix" not in calls
    assert [h["type"] for h in result["handed_back"] if h["row"] == "r0"] == ["rejected-after-retry"]
