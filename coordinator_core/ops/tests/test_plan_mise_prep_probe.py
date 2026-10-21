"""
coordinator_core/ops/tests/test_plan_mise_prep_probe.py — "plan.mise_prep_probe" and
"plan.mise_upgrade_apply".

Subject: `coordinator_core.ops.plan_mise_prep_probe`. Pins the registration surfaces
(registry, scope, classification, budget), the one-reply composition of gate + census +
upgrade derivation, the probe's refusal to write, and the HEAD-keyed corpus cache.

Handlers are synchronous and called directly. Fixtures are bare `tmp_path` trees with a
`.git` directory; HEAD is a hand-written ref so the cache key resolves with no git spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.benchmarks.budget import resolve_budget
from coordinator_core.ops import plan_mise_prep_probe as mod
from coordinator_core.roadmap import prep_gate as pg

PROBE = "plan.mise_prep_probe"
APPLY = "plan.mise_upgrade_apply"
REL = "docs/plans/2026-09-07-fixture.md"

_PREPPED_FM = """census: []
prime_exit_criterion:
  statement: the op reports per class
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""

_DERIVABLE_FM = "sizing_object: state/sizings/x.yaml\n"


def _repo(tmp_path: Path, sha: str = "a" * 40) -> Path:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    (tmp_path / "coordinator_core").mkdir()
    return git


def _plan(root: Path, *, frontmatter: str, spine: str | None, name: str = REL) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ["", "# Fixture", ""]
    if spine is not None:
        body += ["## Tasks", "", "```yaml plan-tasks", spine.strip(), "```", ""]
    path.write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-09-07\n"
        + frontmatter
        + "---\n"
        + "\n".join(body),
        encoding="utf-8",
    )
    return name


def _probe(params: dict, common: Path) -> dict:
    return mod._probe_handler(params, common)


@pytest.fixture(autouse=True)
def _fresh_cache():
    mod._CORPUS_CACHE.clear()
    yield
    mod._CORPUS_CACHE.clear()


@pytest.mark.parametrize("op", [PROBE, APPLY])
def test_each_op_resolves_through_the_real_dispatch_path(op):
    handler = ipc._REGISTRY.get(op) or ipc._lazy_import_and_lookup(op)
    assert callable(handler), f"{op!r} ships present-but-dead"


def test_classifications():
    assert classify(PROBE) is OpClass.COMPUTE_ONLY
    assert classify(APPLY) is OpClass.MUTATING


@pytest.mark.parametrize("op", [PROBE, APPLY])
def test_each_op_is_keyed_common_dir_and_resolves_a_budget(op):
    assert ipc.OP_KEY_SCOPE.get(op) == "common_dir"
    assert resolve_budget(op, classify(op))["target_ms"] > 0


@pytest.mark.parametrize("handler", ["_probe_handler", "_apply_handler"])
def test_absent_repo_root_refuses(handler):
    with pytest.raises(ValueError, match="requires a resolved repo_root"):
        getattr(mod, handler)({"plan": REL}, None)


@pytest.mark.parametrize("bad", [None, "", "  ", 7, ["a"]])
def test_plan_must_name_one_plan(tmp_path, bad):
    common = _repo(tmp_path)
    with pytest.raises(ValueError, match="must be a non-empty string"):
        _probe({"plan": bad}, common)


def test_flags_must_be_booleans(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    with pytest.raises(ValueError, match="check_census must be a boolean"):
        _probe({"plan": rel, "check_census": "yes"}, common)


def test_a_prepped_plan_returns_gate_and_census_and_no_upgrade(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    reply = _probe({"plan": rel, "check_census": True, "upgrade": True}, common)
    assert reply["gate"]["verdict"] == pg.PREPPED
    assert reply["gate"]["plan"] == rel
    assert set(reply["gate"]) == {
        "plan", "verdict", "withheld_rows", "classes", "message", "stamp", "engine_build",
    }
    assert isinstance(reply["census"], dict) and "state" in reply["census"]
    assert reply["upgrade"] is None
    assert reply["process_ms"] >= 0


def test_census_is_not_run_unless_asked(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    assert _probe({"plan": rel}, common)["census"] is None


def test_a_not_prepped_plan_returns_an_upgrade_derivation_and_writes_nothing(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_DERIVABLE_FM, spine=None)
    path = tmp_path / rel
    before = path.read_bytes()
    reply = _probe({"plan": rel, "check_census": True, "upgrade": True}, common)
    assert reply["gate"]["verdict"] == pg.NOT_PREPPED
    assert reply["census"] is None
    assert reply["upgrade"]["changed"] is True
    assert "prime_exit_criterion.derived_from" in reply["upgrade"]["derived"]
    assert reply["upgrade"]["plan"] == rel
    assert path.read_bytes() == before


def test_upgrade_is_not_derived_unless_asked(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_DERIVABLE_FM, spine=None)
    assert _probe({"plan": rel}, common)["upgrade"] is None


def test_a_schema_only_failure_is_skipped_as_not_upgradeable(tmp_path, monkeypatch):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    real = mod.gate_plan_with_corpus

    def schema_only(plan_path, corpus, *, text=None):
        report = real(plan_path, corpus, text=text)
        report["verdict"] = pg.NOT_PREPPED
        report["classes"] = dict(report["classes"])
        report["classes"]["SCHEMA"] = {"status": "DEFECT", "kind": "x", "detail": "d", "withheld": []}
        for name in report["classes"]:
            if name != "SCHEMA":
                report["classes"][name] = {"status": "PASS", "kind": None, "detail": "", "withheld": []}
        return report

    monkeypatch.setattr(mod, "gate_plan_with_corpus", schema_only)
    reply = _probe({"plan": rel, "upgrade": True}, common)
    assert reply["upgrade"] == {"skipped": "not upgradeable"}


def test_two_probes_at_one_head_derive_the_corpus_once(tmp_path, monkeypatch):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    calls = []
    real = mod.corpus_inputs
    monkeypatch.setattr(mod, "corpus_inputs", lambda root: calls.append(root) or real(root))
    _probe({"plan": rel}, common)
    _probe({"plan": rel}, common)
    assert len(calls) == 1


def test_a_head_move_recomputes_the_corpus(tmp_path, monkeypatch):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    calls = []
    real = mod.corpus_inputs
    monkeypatch.setattr(mod, "corpus_inputs", lambda root: calls.append(root) or real(root))
    _probe({"plan": rel}, common)
    (common / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="utf-8")
    _probe({"plan": rel}, common)
    assert len(calls) == 2


def test_an_unreadable_head_is_never_cached(tmp_path, monkeypatch):
    common = _repo(tmp_path)
    (common / "HEAD").unlink()
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    calls = []
    real = mod.corpus_inputs
    monkeypatch.setattr(mod, "corpus_inputs", lambda root: calls.append(root) or real(root))
    _probe({"plan": rel}, common)
    _probe({"plan": rel}, common)
    assert len(calls) == 2


def test_apply_writes_the_derivable_fields(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_DERIVABLE_FM, spine=None)
    path = tmp_path / rel
    before = path.read_text(encoding="utf-8")
    reply = mod._apply_handler({"plan": rel}, common)
    assert reply["changed"] is True
    assert reply["plan"] == rel
    after = path.read_text(encoding="utf-8")
    assert after != before
    assert "census: []" in after
    assert mod._apply_handler({"plan": rel}, common)["derived"] == []


def test_apply_refuses_a_non_plan(tmp_path):
    common = _repo(tmp_path)
    path = tmp_path / "docs" / "plans" / "not-a-plan.md"
    path.parent.mkdir(parents=True)
    path.write_text("no frontmatter here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a plan"):
        mod._apply_handler({"plan": "docs/plans/not-a-plan.md"}, common)


def test_a_plan_outside_the_worktree_is_refused(tmp_path):
    common = _repo(tmp_path)
    outside = tmp_path.parent / "elsewhere-probe.md"
    outside.write_text("---\ntitle: x\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the resolved worktree"):
        _probe({"plan": str(outside)}, common)


CERTIFY = "plan.mise_certify_probe"


def _stamped(root: Path, *, sha: str | None, frontmatter: str = _PREPPED_FM) -> str:
    """A plan whose stamp sha is the body's own (None: the live sha, else the given one)."""
    from coordinator_core.frontmatter.primitives import approval_body_sha

    rel = _plan(root, frontmatter=frontmatter, spine=_SPINE)
    path = root / rel
    text = path.read_text(encoding="utf-8")
    recorded = sha or approval_body_sha(text)
    stamp = (
        f"mise_prepped_by: fixture\nmise_prepped_at: 2026-09-07T00:00:00Z\n"
        f"mise_prepped_sha: {recorded}\nmise_prepped_findings: []\n"
    )
    path.write_text(text.replace("---\n", "---\n" + stamp, 1), encoding="utf-8")
    return rel


class _StubRevalidator:
    def __init__(self):
        self.calls = []

    def revalidate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"state": "MATCH", "entries": []}


def test_certify_is_compute_only_keyed_and_budgeted():
    assert classify(CERTIFY) is OpClass.COMPUTE_ONLY
    assert ipc.OP_KEY_SCOPE.get(CERTIFY) == "common_dir"
    assert resolve_budget(CERTIFY, classify(CERTIFY))["target_ms"] > 0
    handler = ipc._REGISTRY.get(CERTIFY) or ipc._lazy_import_and_lookup(CERTIFY)
    assert callable(handler)


def test_certify_certified_plan_runs_the_census_with_the_default_stamp_rule(tmp_path, monkeypatch):
    common = _repo(tmp_path)
    rel = _stamped(tmp_path, sha=None)
    stub = _StubRevalidator()
    monkeypatch.setattr(mod, "_revalidator", lambda: stub)
    reply = mod._certify_handler({"plan": rel, "census_timeout": 7}, common)
    assert reply["sha"] == "CERTIFIED"
    assert reply["plan"] == rel
    assert reply["census"] == {"state": "MATCH", "entries": []}
    assert reply["stamp"]["state"] == "CERTIFIED"
    assert "sha_detail" not in reply
    assert len(stub.calls) == 1
    (_plan_arg, _root, timeout), kwargs = stub.calls[0]
    assert timeout == 7 and set(kwargs) == {"deadline"}


def test_certify_stale_plan_has_no_census_and_the_detail(tmp_path, monkeypatch):
    common = _repo(tmp_path)
    rel = _stamped(tmp_path, sha="b" * 64)
    stub = _StubRevalidator()
    monkeypatch.setattr(mod, "_revalidator", lambda: stub)
    reply = mod._certify_handler({"plan": rel}, common)
    assert reply["sha"] == "STALE"
    assert reply["census"] is None and not stub.calls
    assert reply["sha_detail"].startswith("recorded " + "b" * 64 + ", body ")


def test_certify_unstamped_plan_has_no_census(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    reply = mod._certify_handler({"plan": rel}, common)
    assert reply["sha"] == "UNSTAMPED"
    assert reply["census"] is None


def test_certify_partial_stamp_is_malformed_with_the_missing_fields(tmp_path):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM + "mise_prepped_by: x\n", spine=_SPINE)
    reply = mod._certify_handler({"plan": rel}, common)
    assert reply["sha"] == "MALFORMED"
    assert reply["sha_detail"].startswith("stamp fields missing: ")
    assert reply["census"] is None


def test_certify_unreadable_plan_is_malformed_with_the_oserror(tmp_path, monkeypatch):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)

    def boom(self, *a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_text", boom)
    reply = mod._certify_handler({"plan": rel}, common)
    assert reply["sha"] == "MALFORMED" and reply["sha_detail"] == "disk gone"
    assert reply["census"] is None


def test_certify_missing_file_is_refused_for_the_client_to_map(tmp_path):
    common = _repo(tmp_path)
    with pytest.raises(ValueError, match="no such plan"):
        mod._certify_handler({"plan": "docs/plans/absent.md"}, common)


@pytest.mark.parametrize("bad", [["docs/plans/a.md"], ["a", "b"], None, ""])
def test_certify_refuses_a_list_or_empty_plan(tmp_path, bad):
    common = _repo(tmp_path)
    with pytest.raises(ValueError, match="must be a non-empty string"):
        mod._certify_handler({"plan": bad}, common)


@pytest.mark.parametrize("bad", [0, -1, True, "5"])
def test_certify_census_timeout_must_be_a_positive_int(tmp_path, bad):
    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE)
    with pytest.raises(ValueError, match="census_timeout must be a positive integer"):
        mod._certify_handler({"plan": rel, "census_timeout": bad}, common)


_SLOW_CENSUS_FM = """census:
  - question: first slow entry
    command: ls
    result: "1"
  - question: second slow entry
    command: ls
    result: "1"
prime_exit_criterion:
  statement: the op reports per class
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""


@pytest.fixture
def slow_shell(tmp_path_factory, monkeypatch):
    """Every census command becomes a 10s sleep: the shell is named by the override knob."""
    script = tmp_path_factory.mktemp("slowshell") / "slow.sh"
    script.write_text("#!/bin/sh\nexec sleep 10\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("COORDINATOR_CENSUS_SHELL", str(script))
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 4.0)


def _assert_deadline_census(census: dict) -> None:
    assert census["state"] == "UNDECIDABLE"
    assert [e["state"] for e in census["entries"]] == ["UNRUNNABLE", "UNRUNNABLE"]
    assert "census deadline was spent" in census["entries"][1]["detail"]


@pytest.mark.skipif(__import__("os").name == "nt", reason="needs a POSIX sh script as the shell")
def test_probe_census_stops_at_the_deadline_under_the_ops_own_timeout(tmp_path, slow_shell):
    import time

    common = _repo(tmp_path)
    rel = _plan(tmp_path, frontmatter=_SLOW_CENSUS_FM, spine=_SPINE)
    started = time.monotonic()
    reply = _probe({"plan": rel, "check_census": True}, common)
    assert time.monotonic() - started < 3.0
    assert reply["gate"]["verdict"] == pg.PREPPED
    _assert_deadline_census(reply["census"])


@pytest.mark.skipif(__import__("os").name == "nt", reason="needs a POSIX sh script as the shell")
def test_certify_census_stops_at_the_deadline_under_the_ops_own_timeout(tmp_path, slow_shell):
    import time

    common = _repo(tmp_path)
    rel = _stamped(tmp_path, sha=None, frontmatter=_SLOW_CENSUS_FM)
    started = time.monotonic()
    reply = mod._certify_handler({"plan": rel}, common)
    assert time.monotonic() - started < 3.0
    assert reply["sha"] == "CERTIFIED"
    _assert_deadline_census(reply["census"])


@pytest.mark.parametrize("op", [PROBE, CERTIFY])
def test_the_deadline_is_read_through_timeout_for(monkeypatch, op):
    seen = []
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: seen.append(method) or 8.0)
    import time

    before = time.monotonic()
    deadline = mod._census_deadline(op)
    assert seen == [op]
    assert deadline - before == pytest.approx(8.0 * mod._CENSUS_DEADLINE_SHARE, abs=0.5)
