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
    guard also stays on the fast tier. This synthetic case is a SINGLE
    block, inline shape only -- the cheapest case, despite an earlier
    revision of its own docstring claiming otherwise (fixed by this
    chunk); the AC9 amendment (a companion-file write is different work
    on the same hot path) is re-priced separately, against the real
    `coordinator:staff-eng` policy row (11 blocks, always spills), by
    `test_compose_catering_process_time_companion_write_widest_type`
    below -- skipped without a sibling DoE-claude checkout, same gate as
    `test_cater_subagent_start.py`'s own real-corpus family.

This file also carries the AC9 cap-invariant regression guard
(`test_every_catered_type_composes_under_the_char_cap`): every catered
type in the real policy's `contract_blocks` map (33 types, not a
hand-picked few) must compose under `ADDITIONAL_CONTEXT_CHAR_CAP` --
the whole failure class this plan exists to close is a payload silently
exceeding the `additionalContext` channel, so this guard fails loudly
rather than assuming the spill arm keeps working.

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

from coordinator_core.hooks.cater_subagent_start import (
    ADDITIONAL_CONTEXT_CHAR_CAP,
    compose_catering,
)
from coordinator_core.testing.doe_root import doe_root_and_present

_MODULE_PATH = Path(__file__).resolve().parents[1] / "cater_subagent_start.py"

#: C3 lever 1 (plan `2026-08-21-catering-costs-what-the-work-costs.md` § C3):
#: `hooks/track_touched_files.py:92` used to import
#: `coordinator_core.ops.session_context` at MODULE scope. Because the
#: `coordinator_core.hooks` package eagerly imports every hook module
#: (including `track_touched_files`) on any `coordinator_core.hooks.*`
#: import -- this file's own `compose_catering` import above triggers that
#: sweep -- that one line dragged the entire ops registry (percolate
#: engine, publish transport, close_out_and_stamp, urllib.request,
#: http.client, yaml) onto every catering fire, a path that never actually
#: calls the op it imported. This regression guard lives beside the C2
#: catering budget file rather than in track_touched_files' own test module
#: because the cost it prices is paid by THIS entrypoint's hot path, not by
#: track_touched_files' own direct callers.
_TRACK_TOUCHED_FILES_PATH = (
    Path(__file__).resolve().parents[1] / "track_touched_files.py"
)


def test_track_touched_files_keeps_ops_session_context_off_module_scope() -> None:
    """C3 lever 1 regression guard: `coordinator_core.ops.session_context`
    must be imported only inside a function body of `track_touched_files.py`,
    never at module (top) scope -- an AST walk over the module's own
    top-level `Import`/`ImportFrom` statements (NOT a raw-text grep, which
    would also match the deliberately-retained function-local import this
    lever depends on). A regression here re-drags the ops registry (and
    everything it imports) back onto the catering hot path this chunk moved
    it off of.
    """
    import ast

    source = _TRACK_TOUCHED_FILES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_TRACK_TOUCHED_FILES_PATH))

    module_scope_hits = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "coordinator_core.ops.session_context"
    ]
    assert not module_scope_hits, (
        "track_touched_files.py imports coordinator_core.ops.session_context "
        "at MODULE scope again -- this re-drags the ops registry (percolate "
        "engine, publish transport, urllib.request, http.client, yaml) onto "
        "every catering fire (C3 lever 1 regression)."
    )

DOE_ROOT, DOE_ROOT_PRESENT = doe_root_and_present()

#: Widest `contract_blocks` row on disk (module docstring's own AC9 finding,
#: cited not re-derived) -- the type this file's re-measurement (secondary
#: guard, companion-write shape) and the cap-invariant sweep both anchor on
#: for their "worst real case" sample.
WIDEST_TYPE = "coordinator:staff-eng"

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
    SINGLE `contract_blocks` entry (`quota-self-detect-preamble`), against
    a synthetic single-block fixture repo -- despite this docstring's
    earlier claim, this is the cheapest inline shape this module composes,
    not the widest; the widest real shape (11 blocks, companion-write) is
    priced separately by
    `test_compose_catering_process_time_companion_write_widest_type`
    below, against the real `coordinator:staff-eng` policy row (this
    chunk's re-measurement, AC6/AC9). Compares against AC6's stated 150ms
    ceiling; no subprocess is spawned to take this measurement (see module
    docstring, "Secondary guard").
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


# ---------------------------------------------------------------------------
# SECONDARY guard, re-priced -- companion-write shape (this chunk's ask)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not DOE_ROOT_PRESENT,
    reason="sibling DoE-claude checkout not resolvable on this machine "
    "(see coordinator_core.testing.doe_root.resolve_doe_root)",
)
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_compose_catering_process_time_companion_write_widest_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6's ceiling was priced against the INLINE shape only (the test
    above, a single block, no spill). A companion-file write is different
    work on the same hot path (AC9 amendment) and fires on every dispatch
    fleet-wide -- this re-measures process time for `WIDEST_TYPE`
    (`coordinator:staff-eng`, 11 real `contract_blocks`, the widest row on
    disk), which composes well over `ADDITIONAL_CONTEXT_CHAR_CAP` and so
    always takes the spill-to-companion-file arm, not merely the widest
    inline case. Min-of-N `time.process_time()` (CPU time, never
    wall-clock) against the real policy/snippet corpus -- the shape the
    fleet actually sees, not a synthetic stand-in. No subprocess is
    spawned to take this measurement (spilling is a plain `open()` write,
    covered by `test_module_source_never_spawns_a_process`'s AST sweep
    over this same module -- 0 additional spawns is not re-measured here).
    """
    import os
    import shutil

    import yaml

    policy_file = Path(DOE_ROOT) / "coordinator" / "subagent-sandbox-policy.yaml"
    policy_data = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    block_names = policy_data["contract_blocks"][WIDEST_TYPE]

    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_file))
    session_id = "budget-companion-write-widest"
    session_dir = Path(DOE_ROOT) / "state" / "subagent-share" / session_id
    payload = {
        "agent_type": WIDEST_TYPE,
        "session_id": session_id,
        "contract_blocks": block_names,
    }

    try:
        # Untimed warm-up, same rationale as the inline case above.
        compose_catering(payload, cwd=DOE_ROOT)

        samples = []
        for _ in range(_SAMPLE_COUNT):
            start = time.process_time()
            compose_catering(payload, cwd=DOE_ROOT)
            samples.append((time.process_time() - start) * 1000.0)
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)
        os.environ.pop("SUBAGENT_SANDBOX_POLICY", None)

    best_ms = min(samples)
    assert best_ms <= _AC6_CEILING_MS, (
        f"compose_catering (companion-write shape, {WIDEST_TYPE}) process "
        f"time {best_ms:.2f}ms exceeds AC6's {_AC6_CEILING_MS}ms ceiling "
        f"(samples={samples!r})"
    )


# ---------------------------------------------------------------------------
# CAP INVARIANT -- every catered type stays under ADDITIONAL_CONTEXT_CHAR_CAP
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not DOE_ROOT_PRESENT,
    reason="sibling DoE-claude checkout not resolvable on this machine "
    "(see coordinator_core.testing.doe_root.resolve_doe_root)",
)
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_every_catered_type_composes_under_the_char_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9's cap regression guard: enumerate EVERY catered type from the
    real DoE policy map (`subagent-sandbox-policy.yaml`'s `contract_blocks`
    keys -- the same 33-type population the EM's own measurement swept,
    not a hand-picked few) and assert each type's composed
    `additionalContext` TOTAL is at or under `ADDITIONAL_CONTEXT_CHAR_CAP`.

    This does not merely re-check the spill mechanism fires for the
    already-known-wide rows -- it is the regression guard against a FUTURE
    block relocation (a `contract_blocks` edit that moves blocks onto a
    type currently under the cap, or a bug in the spill arm that leaves a
    type's total silently over cap despite `compose_catering`'s fail-open
    contract) going undetected. The failure class this plan exists to
    close is a payload silently exceeding the `additionalContext` channel;
    this test is the loud version of that failure.
    """
    import shutil

    import yaml

    policy_file = Path(DOE_ROOT) / "coordinator" / "subagent-sandbox-policy.yaml"
    policy_data = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    catered_types = policy_data["contract_blocks"]
    assert catered_types, "real policy's contract_blocks map is unexpectedly empty"

    # The sweep resolves blocks and role framing against the REAL sibling
    # checkout, so `cwd` has to be DOE_ROOT -- there is no synthetic corpus
    # that would measure the real population. That makes the sidecar and
    # companion writes land in a PEER's working tree, which is tracked, not
    # ignored: every directory this loop creates is reconciliation work for
    # whoever runs `git status` over there next. So record each session
    # directory as it is minted and remove exactly those. A single
    # sweep-wide root would be wrong -- `_provision` keys the directory on
    # `session_id`, one per type, and cleaning a root that is never created
    # leaves the real ones behind.
    session_dirs: list[Path] = []
    share_root = Path(DOE_ROOT) / "state" / "subagent-share"
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_file))
    try:
        over_cap: list[tuple[str, int]] = []
        for agent_type, block_names in catered_types.items():
            session_id = f"budget-cap-invariant-{_sanitize(agent_type)}"
            session_dirs.append(share_root / session_id)
            payload = {
                "agent_type": agent_type,
                "session_id": session_id,
                "contract_blocks": block_names,
            }
            result = compose_catering(payload, cwd=DOE_ROOT)
            if len(result) > ADDITIONAL_CONTEXT_CHAR_CAP:
                over_cap.append((agent_type, len(result)))

        assert not over_cap, (
            f"{len(over_cap)} of {len(catered_types)} catered type(s) composed "
            f"OVER the {ADDITIONAL_CONTEXT_CHAR_CAP}-char cap: {over_cap!r}"
        )
    finally:
        for session_dir in session_dirs:
            shutil.rmtree(session_dir, ignore_errors=True)


def _sanitize(agent_type: str) -> str:
    """Filesystem/session-id-safe stand-in for a `coordinator:foo` agent
    type -- mirrors `_sanitize_segment`'s own colon-hostility (Windows
    reserves `:` in a path segment) without importing that private helper
    across a package boundary for a single character swap."""
    return agent_type.replace(":", "-")
