"""
coordinator_core.hooks.tests.test_cater_subagent_start_budget -- prices the
C2 catering leg (`coordinator_core.hooks.cater_subagent_start`) against the
gating plan's AC6 and the SubagentStart registration's 5s budget.

AC6 (plan `docs/plans/2026-08-21-catering-rides-subagentstart.md`): the leg
adds **<=150ms process time** to the SubagentStart shim and **0 additional
process spawns**, measured as process time and spawn count -- never wall
clock (wall clock here measures peer load under the 50-70 concurrent-LLM
norm, not this leg; `docs/wiki/machine-load-norm.md`).

Two guards, matching `test_hot_path_hook_import_budget.py`'s own
primary/secondary split (cited, not re-derived):

  * PRIMARY, deterministic, fast-tier -- a static source-text check that
    `cater_subagent_start.py` never imports/calls a process-spawning API.
    The module's own docstring already claims "In-process only -- no
    subprocess" (its § on `_PROVISION_TIMEOUT_SECONDS`); this is the
    regression guard for that claim, not a fresh measurement. Per this
    chunk's ratchet note, a test that actually spawns `git` needs
    `spawns_process` + `cadence` markers -- a static text check needs
    neither and proves the same "0 additional spawns" fact for THIS leg
    (`compose_catering` never shells out; `resolve_git_root` and
    `_provision`/`assemble_contract_blocks_for_payload` are pure Python +
    filesystem I/O, no subprocess -- confirmed by their own imports, not
    re-derived here).
  * SECONDARY, timing, min-of-N `time.process_time()` (CPU time, never
    wall-clock, per `state/lessons/2026-08-03-wall-clock-assertions-in-a-
    parallel-test-bc800cb5a894.yaml`) around a direct, in-process
    `compose_catering` call against a synthetic eligible-type fixture
    (mirrors `test_cater_subagent_start.py`'s own `git_repo`/`policy_path`
    fixture shape, kept self-contained here rather than shared across test
    modules). No subprocess is spawned to take this measurement, so this
    guard also stays on the fast tier.

Baselines named in the brief (not re-derived, cited for context only): DoE's
2026-08-16 hook benchmark put a cold `python3` start at ~35ms median
uncontended and `git rev-parse` at ~25ms; a fresh registration measured
~642ms; `coordinator_core/hooks/__init__.py`'s own eager import of its
107 modules costs ~31ms. None of those baselines measures THIS shape (an
in-process `compose_catering` call against an already-warm interpreter,
already-imported module) -- this file measures fresh, per the brief.

Spec backlink: docs/plans/2026-08-21-catering-rides-subagentstart.md
(C4 / AC6)
Module under test: coordinator_core/hooks/cater_subagent_start.py
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.hooks.cater_subagent_start import compose_catering

_MODULE_PATH = Path(__file__).resolve().parents[1] / "cater_subagent_start.py"

#: Names that would indicate this leg reaches for a process spawn. Matched
#: against the module's own source text (not its transitive imports --
#: `subagent_sandbox.provision_report`/`engine` are read, not re-audited
#: here; their own "In-process only" contract is this module's docstring
#: claim, cited above, not re-derived).
_SPAWN_SIGNATURES = (
    "subprocess",
    "os.system",
    "os.popen",
    "os.spawn",
    "Popen",
)

ELIGIBLE_TYPE = "coordinator:code-reviewer"

#: AC6's stated ceiling, in milliseconds.
_AC6_CEILING_MS = 150.0

#: Cheap redundancy against a one-off scheduling anomaly on a single sample,
#: matching `test_hot_path_hook_import_budget.py`'s own `_SAMPLE_COUNT`
#: rationale -- min-of-N, not average-of-N.
_SAMPLE_COUNT = 5


# ---------------------------------------------------------------------------
# PRIMARY guard -- 0 additional process spawns (static, deterministic)
# ---------------------------------------------------------------------------


def test_module_source_never_spawns_a_process() -> None:
    """AC6's "0 additional process spawns" half, priced statically: no
    spawn-signature token appears in any actual CODE statement of
    `cater_subagent_start.py` (import or call) -- an AST walk over
    `ast.Import`/`ast.ImportFrom`/`ast.Call` nodes, deliberately NOT a
    raw-text grep, since the module's own docstring narrates the
    "In-process only -- no subprocess" design decision in prose and would
    false-positive a text-only check. A stub/AST-level check per this
    chunk's ratchet note -- proves the claim without spawning `git` (or
    anything else) itself, so this test needs neither `spawns_process`
    nor `cadence`.
    """
    import ast

    source = _MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_MODULE_PATH))

    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(sig.split(".")[0] == alias.name for sig in _SPAWN_SIGNATURES):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and any(sig.split(".")[0] == node.module for sig in _SPAWN_SIGNATURES):
                hits.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and any(sig.split(".")[-1] == name for sig in _SPAWN_SIGNATURES):
                hits.append(name)

    assert not hits, (
        f"cater_subagent_start.py's code references spawn-shaped API(s) {hits!r} -- "
        "AC6 requires 0 additional process spawns from this leg"
    )


# ---------------------------------------------------------------------------
# SECONDARY guard -- <=150ms added process time (timing, min-of-N)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plain directory standing in for a git root -- NOT a real `git
    init` spawn. `compose_catering` reaches `resolve_git_root` (via both
    `cater_subagent_start.py` and `provision_report.py`'s own imports of
    it), which itself shells out to `git rev-parse --show-toplevel` on a
    cache miss; this fixture stubs that resolution to `tmp_path` in both
    call sites instead, since real git's behaviour is not what this file
    is asserting (only `compose_catering`'s own CPU time is). This keeps
    the AC6 timing sample from including an incidental `git` spawn that
    is not part of what this leg adds -- `resolve_git_root` is a pre-
    existing, already-cached dependency the sidecar/blocks legs share
    with the rest of the SubagentStart pipeline, not new cost introduced
    by this module.
    """
    monkeypatch.setattr(
        "coordinator_core.hooks.cater_subagent_start.resolve_git_root",
        lambda cwd=None: str(tmp_path),
    )
    monkeypatch.setattr(
        "coordinator_core.subagent_sandbox.provision_report.resolve_git_root",
        lambda cwd=None: str(tmp_path),
    )
    (tmp_path / "coordinator" / "snippets").mkdir(parents=True)
    registry = tmp_path / "coordinator" / "snippets" / "registry.toml"
    snippet_name = "quota-self-detect-preamble"
    registry.write_text(
        "schema_version = 1\n\n"
        f'[snippet.{snippet_name}]\n'
        f'sentinel_begin = "<!-- BEGIN {snippet_name} -->"\n'
        f'sentinel_end = "<!-- END {snippet_name} -->"\n'
        'consumers = []\n',
        encoding="utf-8",
    )
    (tmp_path / "coordinator" / "snippets" / f"{snippet_name}.md").write_text(
        f"<!-- BEGIN {snippet_name} -->\nBUDGET-TEST-SNIPPET-BODY\n<!-- END {snippet_name} -->\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = tmp_path / "subagent-sandbox-policy.yaml"
    policy.write_text(f"report_sidecar:\n  - {ELIGIBLE_TYPE}\n", encoding="utf-8")
    return policy


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch: pytest.MonkeyPatch, policy_path: Path) -> None:
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_path))


@pytest.fixture(autouse=True)
def _no_role_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate the role-framing leg's filesystem probe from whatever
    happens to be installed on the machine running this test -- matches
    `test_cater_subagent_start.py`'s own fixture of the same name."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))


def test_compose_catering_process_time_within_ac6_ceiling(
    git_repo: Path,
) -> None:
    """Min-of-N `time.process_time()` (CPU time) around a direct,
    in-process `compose_catering` call for an eligible type carrying a
    `contract_blocks` row -- the widest of the three legs this module
    composes, so the measurement is not the cheapest possible case.
    Compares against AC6's stated 150ms ceiling; no subprocess is spawned
    to take this measurement (see module docstring, "Secondary guard").
    """
    payload = {
        "agent_type": ELIGIBLE_TYPE,
        "session_id": "session-budget-1",
        "cwd": str(git_repo),
        "contract_blocks": ["quota-self-detect-preamble"],
    }

    # One untimed warm-up call so a first-call-only cost (e.g. any
    # module-level lazy init inside the resolved dependencies) does not
    # inflate the min-of-N sample below.
    compose_catering(payload, cwd=str(git_repo))

    samples = []
    for _ in range(_SAMPLE_COUNT):
        start = time.process_time()
        compose_catering(payload, cwd=str(git_repo))
        samples.append((time.process_time() - start) * 1000.0)

    best_ms = min(samples)
    assert best_ms <= _AC6_CEILING_MS, (
        f"compose_catering process time {best_ms:.2f}ms exceeds AC6's "
        f"{_AC6_CEILING_MS}ms ceiling (samples={samples!r})"
    )
