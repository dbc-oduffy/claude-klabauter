import re
from pathlib import Path

import pytest


#: The suite-wide autouse isolation for this directory (_no_ambient_plugin_root,
#: _unhoisted_ask_scripts, _no_ambient_box_terms, _write_set_reads_clean) lives in
#: coordinator_core/conftest.py: a Package-level conftest's fixtures detach under interleaved
#: collection. Guard: test_dispatch_emit_fixtures_attach.py.


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "xs_gate_live: opt out of this directory's stub of the XS diff-budget refusal "
        "(terminal_commit tests that pin the unmeasurable-diff refusal live)",
    )
    config.addinivalue_line(
        "markers",
        "absent_run_script_live: opt out of this directory's run-script write for staged manifests "
        "(tests that pin the absent-run-script refusal)",
    )
    config.addinivalue_line(
        "markers",
        "twin_needs_script: opt out of this directory's run-script stub (the staged-twin refusal of an absent script runs live)",
    )


@pytest.fixture(autouse=True)
def _payload_files_unlinked_per_test():
    from coordinator_core.ops.dispatch_emit.tests._node_support import release_payload_files

    yield
    release_payload_files()


@pytest.fixture(autouse=True)
def _xs_diff_budget_gate_off_outside_its_own_tests(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal_commit fixture cites an XS sizing but names no run base, so the XS diff-budget
    gate refuses it as unmeasurable before the behaviour under test. A module marked
    `xs_gate_live` exercises the gate live."""
    if request.node.get_closest_marker("xs_gate_live"):
        return
    from coordinator_core.ops.dispatch_emit import terminal_commit

    monkeypatch.setattr(terminal_commit, "_xs_over_budget_refusal", lambda *a, **k: None)


_V5_ROSTER_FRAGMENT = {
    "schema": "review-roster-fragment",
    "schema_version": 5,
    "execute_review": {
        "stages": [
            {
                "kind": "prep",
                "agents": [
                    {
                        "agentType": "coordinator:review-prep",
                        "model": "sonnet",
                        "effort": "low",
                        "schema": "prep",
                    }
                ],
            },
            {
                "kind": "review-wave",
                "agents": [
                    {
                        "agentType": "coordinator:code-reviewer",
                        "model": "opus",
                        "effort": "low",
                        "per": "whole-diff",
                        "schema": "wave",
                    }
                ],
            },
            {
                "kind": "integration",
                "agents": [
                    {
                        "agentType": "coordinator:integrator",
                        "model": "opus",
                        "effort": "low",
                        "schema": "integration",
                    }
                ],
            },
        ]
    },
}

_V5_STAGE_SCHEMAS = {
    "prep": {"type": "object"},
    "wave": {"type": "object"},
    "integration": {"type": "object"},
}

#: Keyword arguments every compose_script/emit_script call needs: an emitted
#: workflow always carries the review wave, so no caller may omit them.
REVIEW_KW = {
    "review_roster_fragment": _V5_ROSTER_FRAGMENT,
    "review_stage_schemas": _V5_STAGE_SCHEMAS,
}


def write_run_scripts(root, *run_ids: str) -> None:
    """Write `scratch/warp/<run_id>.workflow.mjs` for each run id under `root`.

    The staged-twin write refuses a run manifest whose launcher script is absent, and the
    engine emits the script before staging; a fixture that stages a run does the same."""
    warp = root / "scratch" / "warp"
    warp.mkdir(parents=True, exist_ok=True)
    for run_id in run_ids:
        (warp / f"{run_id}.workflow.mjs").write_text("export const meta = { name: 'x' }\nreturn 1;\n", encoding="utf-8")


_LIVE_TWIN_REFUSAL_MODULES = frozenset({"test_reply_sidecar"})


@pytest.fixture(autouse=True)
def _stage_finds_its_run_script(request: pytest.FixtureRequest):
    """Cluster B: staging a run manifest leaves a twin beside the run's script and refuses when the
    script is absent. A fixture that stages `run_id` without emitting the script gets the same
    `write_run_scripts` stub the explicit callers use, written only when the file is missing.
    A test marked `twin_needs_script` or `absent_run_script_live`, or in a module of `_LIVE_TWIN_REFUSAL_MODULES` (they pin the
    refusal itself), keeps it live."""
    if request.node.get_closest_marker("twin_needs_script") or request.node.get_closest_marker("absent_run_script_live") or request.module.__name__.rpartition(".")[2] in _LIVE_TWIN_REFUSAL_MODULES:
        yield
        return
    from coordinator_core.ops.dispatch_emit import launcher_staged_twin as twin

    real = twin.ensure_twin

    def _with_script(root, sidecar_rel, *args, **kwargs):
        run_id = twin.manifest_run_id(sidecar_rel)
        if run_id is not None and not (Path(root) / "scratch" / "warp" / f"{run_id}.workflow.mjs").exists():
            write_run_scripts(Path(root), run_id)
        return real(root, sidecar_rel, *args, **kwargs)

    # Not monkeypatch: a test that calls `monkeypatch.undo()` mid-test must not strip the stub.
    twin.ensure_twin = _with_script
    try:
        yield
    finally:
        twin.ensure_twin = real


_SHARED_DECL = re.compile(
    r"^ *const (?:_PF\w+|_RX\d+|_agentHead\d*|_agentRead|_testOwed|_testUnproven|_locatePlan|_owedHandback|_targetedTests|_docPaths|_testRan) = .*;$",
    re.M,
)


def shared_decls(script: str) -> str:
    """The composer's shared top-level helpers (regex literals, prompt openings, repeated expressions) as the script declares them.

    A harness that runs a carved fragment of the script prepends this, as it prepends its own stubs of the
    run-time bindings the fragment reads.
    """
    return "\n".join(m.group(0).strip() for m in _SHARED_DECL.finditer(script))


def unshare_regex_literals(script: str) -> str:
    """Inline each shared `_RXn` regex constant back into its use sites, so a test reads the literal the composer wrote."""
    for m in re.finditer(r"^const (_RX\d+) = (/.*/[a-z]*);\n", script, re.M):
        script = script.replace(m.group(0), "").replace(m.group(1), m.group(2))
    return script


def unshare_agent_head(script: str) -> str:
    """Inline each shared `_agentHead*` arrow back into its call sites, so a test reads the prompt text a leg carries."""
    for m in re.finditer(r"^const (_agentHead\d*) = \(\) => (.*);\n", script, re.M):
        script = script.replace(m.group(0), "").replace(f"{m.group(1)}()", m.group(2))
    return script


_BLITZ_DECL = re.compile(r"^const _blitzCall = \(_pp\) => (planBlitz\(\{.*?planPath: )_pp(, executionOpen: true \}\] \}\));\n", re.M)


def unshare_blitz_call(script: str) -> str:
    """Inline the shared `_blitzCall(planPath)` arrow back into each call site, so a test reads the `planBlitz({...})` text a leg carries."""
    m = _BLITZ_DECL.search(script)
    if not m:
        return script
    head, tail = m.group(1), m.group(2)
    body = script[: m.start()] + script[m.end() :]
    return re.sub(r"_blitzCall\((.+?)\);", lambda c: head + c.group(1) + tail + ";", body)


def compose_ask_script(*args, **kwargs) -> str:
    """`compose_ask_script` with the shared plan-blitz call inlined; for tests that assert on that call's text."""
    from coordinator_core.ops.dispatch_emit import ask_compose

    return unshare_blitz_call(ask_compose.compose_ask_script(*args, **kwargs))


def execute_section(script: str) -> str:
    """The emitted script up to its review wave, for assertions about the
    execute phase that the review wave's own agents would otherwise satisfy."""
    return script.split("phase('Review prep')", 1)[0]


@pytest.fixture
def m_plus_known_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose as an `--ask --sizing` M+ re-fire: only a known M+/roadmap arm embeds plan-blitz,
    so a test reading the embedded planBlitz body opts in here rather than composing a raw ask."""
    from coordinator_core.ops.dispatch_emit import ask_compose

    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_M_PLUS)


@pytest.fixture
def verifier_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    """DoE's live plan-route roster plus a delivery-verifier. The live roster names none
    (delivery-exempt), so a test of the verifier's brief has to add one."""
    import copy

    from coordinator_core.ops.dispatch_emit import emission_receipt
    from coordinator_core.ops.review_mint.tests.test_execute_review import _v5_fragment

    live = emission_receipt._load_review_inputs
    verifier = next(
        a for s in _v5_fragment()["execute_review"]["stages"] if s["kind"] == "review-wave"
        for a in s["agents"] if a.get("agentType") == "coordinator:delivery-verifier"
    )

    def _with_verifier(route):
        fragment, schemas = live(route)
        fragment = copy.deepcopy(fragment)
        wave = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
        wave["agents"].append({**verifier, "schema": "delivery-verdict-result"})
        return fragment, schemas

    monkeypatch.setattr(emission_receipt, "_load_review_inputs", _with_verifier)
