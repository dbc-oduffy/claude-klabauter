"""
coordinator_core.ops.tests.test_withhold_registration_census — a mechanical census over
withhold-shaped assertions, so an unregistered withhold stops being invisible until someone
happens to grep for it.

PURPOSE
`emit_withheld_knobs.py :: _registry` is hand-declared: a site that withholds a knob from
agent-facing text is supposed to register an entry beside the code that withholds it
(`docs/reference/withheld-knobs.md`). Nothing enforced that until this module. On
2026-09-04 a manual grep found a real unregistered case
(`coordinator/bin/tests/test_percolate_round_lock_denies_fast.py:111`) inside the artifact
built that same day to answer this problem — closed by hand at `7b3f55b264`. The next one
will not be found by someone happening to grep.

THE DESIGN CONSTRAINT THAT DECIDES EVERYTHING
A guard that reds on correct code gets suppressed, and a suppressed guard is worse than
none — roughly half of the withhold-shaped assertions in this repo are NOT withholds at
all (`os.environ`/`env`/`captured[...]`-shaped checks about the process environment or
generated text). THE DISCRIMINATOR IS THE HAYSTACK an assertion checks against, never the
knob name: `_classify_haystack` below is the one place that decision is made, and every
other function in this module is bounded scanning/reporting around it.

NEGATIVE SPEC
  - NOT a second registry. This module reads `emit_withheld_knobs._registry()`; it never
    declares a knob-to-path mapping of its own.
  - NOT a name-existence check. It classifies assertions already present in test source; it
    does not decide whether a knob *should* be withheld anywhere.
  - An unrecognised haystack is never silently treated as "not a withhold" — that decay path
    is exactly how this class of guard dies without anyone noticing. It is reported and
    fails loudly (`test_no_unclassified_haystacks_over_the_real_tree`).

Reference: docs/reference/withheld-knobs.md
Plan:      docs/plans/2026-09-04-an-unregistered-withhold-fails-the-suite.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from coordinator_core.ops import emit_withheld_knobs as ewk

# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

#: `assert "COORDINATOR_..." not in <haystack>` — the whole withhold-shaped family. The
#: haystack token is captured permissively (identifiers, dots, brackets, quoted subscripts)
#: because the classifier below, not this regex, carries the discriminating judgment.
_ASSERT_RE = re.compile(
    r'assert\s+"(COORDINATOR_[A-Z0-9_]+)"\s+not\s+in\s+([A-Za-z_][A-Za-z0-9_.\[\]"\']*)'
)

#: Directories this census scans, relative to the repo root. Named, not `**/*.py` over the
#: whole tree, so the question stays bounded to where withhold-shaped assertions live.
_SCAN_DIRS = ("coordinator", "coordinator_core")


@dataclass(frozen=True)
class Finding:
    """One `assert "COORDINATOR_..." not in <haystack>` site."""

    relpath: str  # POSIX-style, repo-root-relative — stable across OS and CWD
    lineno: int
    knob: str
    haystack: str
    line: str


def _repo_root() -> Path:
    """Repo root derived from `__file__`, never a hardcoded path or cwd assumption."""
    return Path(__file__).resolve().parents[3]


def scan_dir_for_findings(root: Path, scan_dirs: Tuple[str, ...] = _SCAN_DIRS) -> List[Finding]:
    """Scan `scan_dirs` under `root` for withhold-shaped assertions.

    Pure over its `root` argument (never reads `_repo_root()` itself) so C3's synthetic-tree
    probes can call it against a `tmp_path` fixture instead of the real tree.
    """
    findings: List[Finding] = []
    for scan_dir in scan_dirs:
        base = root / scan_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            relpath = path.relative_to(root).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                match = _ASSERT_RE.search(line)
                if not match:
                    continue
                knob, haystack = match.group(1), match.group(2)
                findings.append(Finding(relpath, lineno, knob, haystack, line.strip()))
    return findings


# ---------------------------------------------------------------------------
# The classifier — the one place the discriminating judgment lives
# ---------------------------------------------------------------------------

#: Agent-facing haystacks: assertions ABOUT text an agent could read (a refusal, an
#: advisory, a rendered message). A match here IS a withholding assertion.
_AGENT_FACING_EXACT = frozenset({"err", "msg", "stderr", "reason", "note", "text", "ctx", "result"})
_AGENT_FACING_SUFFIXES = ("_note", "_msg", "_err", "_ctx")

#: Environment/dict/generated-text haystacks: assertions about the PROCESS ENVIRONMENT,
#: a captured dict, generated hook text, or — see the `"code"` entry below — a source-code
#: literal-hygiene check. None of these are statements about agent-facing refusal text, so
#: none are ever flagged.
#:
#: `"code"` is a deliberate, narrow addition beyond the plan's own worked examples:
#: `test_emit_withheld_knobs.py :: test_no_knob_name_is_hardcoded_in_the_emitter_source`
#: asserts a knob name is absent from `_registry`'s SOURCE LINES (`inspect.getsource(...)`,
#: split on `#`) — a DRY/no-hardcoded-literal check, not a claim about what text reaches an
#: agent. It shares no semantics with the withheld-knob doctrine at all.
_NOT_WITHHOLD_EXACT = frozenset({"os.environ", "env", "base_env", "hooks_text", "code"})
_NOT_WITHHOLD_PREFIXES = ("captured[",)
_NOT_WITHHOLD_SUFFIXES = ("_env",)

WITHHOLD = "withhold"
NOT_WITHHOLD = "not-withhold"
UNCLASSIFIED = "unclassified"


def classify_haystack(haystack: str) -> str:
    """Classify a haystack token as WITHHOLD, NOT_WITHHOLD, or UNCLASSIFIED.

    Order matters: the not-withhold table is checked first because a name can collide in
    spirit with the agent-facing table (`hooks_text` contains "text") without being one —
    the FULL token decides, never a loose substring test.
    """
    if haystack in _NOT_WITHHOLD_EXACT:
        return NOT_WITHHOLD
    if haystack.startswith(_NOT_WITHHOLD_PREFIXES):
        return NOT_WITHHOLD
    if haystack.endswith(_NOT_WITHHOLD_SUFFIXES):
        return NOT_WITHHOLD

    if haystack in _AGENT_FACING_EXACT:
        return WITHHOLD
    if haystack.endswith(_AGENT_FACING_SUFFIXES):
        return WITHHOLD

    return UNCLASSIFIED


# ---------------------------------------------------------------------------
# The waiver table (C2) — the escape hatch for a genuine, deliberately-unregistered withhold
# ---------------------------------------------------------------------------
#
# The plan's own C2 body asks for an INLINE marker on the assertion line, in the file that
# holds the assertion. This dispatch's file scope covers exactly two files (this module and
# the reference page) — editing the ten-plus target test files is out of scope. The table
# below is the in-scope substitute: living beside the classification logic it excuses (not a
# drifted-away central list an unrelated file points at), each entry citing the reason and
# the sizing object that decided it. A future waiver belongs here on the same terms, not
# folded silently into the classifier.
#
# KEYED BY (relpath, knob), NOT (relpath, lineno). Line-number keying was tried first and
# rejected: a line inserted anywhere above a waived assertion shifts its lineno, the waiver
# silently stops matching, and the census REDS on an untouched, still-legitimate assertion —
# the exact false-positive-on-correct-code failure this guard exists not to have. Worse, an
# unrelated assertion could drift onto the vacated line number and be silently waived in the
# waived assertion's place. `(relpath, knob)` is stable across edits to the file, and the
# precision loss it trades for that stability is acceptable: it waives every assertion of
# that knob in that file, which is what the waiver reason actually claims — the exemption is
# about the knob's FAMILY on that path, never about one specific source line.
# `test_probe_line_insertion_above_a_waiver_does_not_unwaive_it` below pins this.
#
# Two reason categories, not one string reused for both:
#
# 1. OVERRIDE-WITHHOLDING family (10 files) — declined for the registry on its own merits
#    (state/sizings/2026-09-04-an-unregistered-withhold-fails-the-suite.yaml, `em_analysis`):
#    a wildcard withhold with a live exception (`block_subagent_plan_body_bash_write.py`
#    DOES honor `COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY`), declared in a docstring rather
#    than a constant. Forcing it into the registry to make this guard green was explicitly
#    ruled out by the plan's Anti-scope; waiving is the correct disposition.
# 2. Registry-scope family — `COORDINATOR_SCHEMA_STRICT` in
#    `test_validate_frontmatter_schema_advisory.py`. This is a genuine agent-facing withhold
#    on the frontmatter-validation guard path, but the registry deliberately covers exactly
#    ONE path today (`percolate.destination-lock-refusal`) — this path is not in scope for
#    the registry, which is a different standing than an unregistered withhold on a covered
#    path. Settled by EM decision 2026-09-04; not registered, not provisional.
_WAIVERS: Dict[Tuple[str, str], str] = {
    ("coordinator/bin/tests/test_coordinator_safe_commit_pathspec_ownership_gate.py", "COORDINATOR_OVERRIDE_SCOPE"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_block_stash_destruction.py", "COORDINATOR_OVERRIDE"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_branch_creation_seam_no_hatch_and_ceremony_pins.py", "COORDINATOR_OVERRIDE_BRANCH"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_commit_tripwires.py", "COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_guard_grep_via_bash.py", "COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_guard_longlived_branch_naming.py", "COORDINATOR_OVERRIDE"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_guard_longlived_branch_naming.py", "COORDINATOR_ALLOW"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_guard_longlived_branch_naming.py", "COORDINATOR_DISABLE"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_guard_message_register_lint.py", "COORDINATOR_OVERRIDE_NO_VERIFY"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_operator_override_note_no_assignment_form.py", "COORDINATOR_ALLOW_BARE_NAME_CHECK"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/bash_guards/tests/test_operator_override_note_no_assignment_form.py", "COORDINATOR_QUEUE_PUNT_BARE_NAME_CHECK"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/write_guards/tests/test_block_confined_agent_write.py", "COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/write_guards/tests/test_validate_frontmatter_schema_deny.py", "COORDINATOR_OVERRIDE_OWN_INBOX"):
        "OVERRIDE-WITHHOLDING family, declined for the registry (sizing em_analysis)",
    ("coordinator_core/write_guards/tests/test_validate_frontmatter_schema_advisory.py", "COORDINATOR_SCHEMA_STRICT"):
        "withhold on a path the registry does not cover (registry covers percolate.destination-lock-refusal only)",
}


def registered_knobs() -> frozenset:
    """The set of knob names declared in `emit_withheld_knobs._registry()`."""
    return frozenset(entry["knob"] for entry in ewk._registry())  # noqa: SLF001


@dataclass(frozen=True)
class CensusResult:
    withheld: List[Finding]
    not_withheld: List[Finding]
    unclassified: List[Finding]
    unregistered: List[Finding]  # subset of withheld: no registry entry, no waiver
    waived: List[Finding]  # subset of withheld: no registry entry, but waived


def run_census(findings: List[Finding], registered: frozenset, waivers: Dict[Tuple[str, str], str]) -> CensusResult:
    """Classify every finding and split withholds into registered/waived/unregistered.

    Pure over its arguments so C3's probes can feed a synthetic `findings` list without
    touching the real tree or the real registry.
    """
    withheld: List[Finding] = []
    not_withheld: List[Finding] = []
    unclassified: List[Finding] = []
    unregistered: List[Finding] = []
    waived: List[Finding] = []

    for f in findings:
        cls = classify_haystack(f.haystack)
        if cls == NOT_WITHHOLD:
            not_withheld.append(f)
            continue
        if cls == UNCLASSIFIED:
            unclassified.append(f)
            continue
        withheld.append(f)
        if f.knob in registered:
            continue
        if (f.relpath, f.knob) in waivers:
            waived.append(f)
        else:
            unregistered.append(f)

    return CensusResult(withheld, not_withheld, unclassified, unregistered, waived)


def _census_report(result: CensusResult) -> str:
    """The failure text's census line — counts per class plus the offending lines.

    Register per docs/wiki/guard-messaging.md § Register: WHAT HAPPENED once, no
    self-legitimacy, no repetition, no reassurance.
    """
    lines = [
        f"withhold-registration census: {len(result.withheld)} withheld, "
        f"{len(result.not_withheld)} not-withheld, {len(result.unclassified)} unclassified, "
        f"{len(result.waived)} waived, {len(result.unregistered)} unregistered.",
    ]
    if result.unregistered:
        lines.append("Unregistered (register the knob, or waive with a reason):")
        for f in result.unregistered:
            lines.append(f"  {f.relpath}:{f.lineno} {f.knob} not in {f.haystack}")
    if result.unclassified:
        lines.append("Unclassified haystack (extend classify_haystack deliberately):")
        for f in result.unclassified:
            lines.append(f"  {f.relpath}:{f.lineno} {f.knob} not in {f.haystack}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The real-tree tests (exit criterion #1 and #3)
# ---------------------------------------------------------------------------


def _real_findings() -> List[Finding]:
    return scan_dir_for_findings(_repo_root())


def test_no_unregistered_withholds_over_the_real_tree():
    result = run_census(_real_findings(), registered_knobs(), _WAIVERS)
    assert not result.unregistered, _census_report(result)


def test_no_unclassified_haystacks_over_the_real_tree():
    result = run_census(_real_findings(), registered_knobs(), _WAIVERS)
    assert not result.unclassified, _census_report(result)


def test_real_tree_census_is_fully_classified_and_reported():
    """The exit-criterion #3 requirement stated as a test: every real-tree finding lands in
    exactly one class, none unclassified, and the classification is printed for eye-check."""
    result = run_census(_real_findings(), registered_knobs(), _WAIVERS)
    total = len(result.withheld) + len(result.not_withheld) + len(result.unclassified)
    assert total == len(_real_findings())
    assert not result.unclassified
    print(_census_report(result))


# ---------------------------------------------------------------------------
# C3 — prove the instrument in both directions, over a synthetic corpus (never the real tree)
# ---------------------------------------------------------------------------


#: Builds a probe source file's single assertion line PROGRAMMATICALLY rather than as a
#: literal `assert "COORDINATOR_..." not in ...` triple-quoted string in THIS module's own
#: source — this file itself is under `coordinator_core/` and is scanned by the real-tree
#: tests above. A literal probe line here would be found by its own scan and misreported as
#: a genuine unregistered/unclassified finding in this file. Composing the line from parts
#: keeps the probe text out of the regex's view of this module while still producing an
#: identical assertion line in the synthetic file the probe writes to `tmp_path`.
_ASSERT_KEYWORD = "assert"
_NOT_IN = "not" + " " + "in"


def _probe_assertion_line(knob: str, haystack: str) -> str:
    return f'{_ASSERT_KEYWORD} "{knob}" {_NOT_IN} {haystack}'


def _write_probe(tmp_path: Path, setup: str, knob: str, haystack: str) -> None:
    body = "def test_x():\n"
    if setup:
        body += f"    {setup}\n"
    body += f"    {_probe_assertion_line(knob, haystack)}\n"
    p = tmp_path / "coordinator_core" / "probe" / "test_probe.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_probe_unregistered_withhold_on_covered_path_is_red(tmp_path):
    """An agent-facing assertion for a knob absent from the registry, with no waiver, must
    fail as unregistered. This is the exact shape of the 2026-09-04 gap
    (`test_percolate_round_lock_denies_fast.py:111`) reproduced synthetically."""
    _write_probe(tmp_path, 'err = "refusal text"', "COORDINATOR_NOT_REGISTERED_ANYWHERE", "err")
    findings = scan_dir_for_findings(tmp_path, scan_dirs=("coordinator_core",))
    result = run_census(findings, registered_knobs(), waivers={})
    assert len(result.unregistered) == 1
    assert result.unregistered[0].knob == "COORDINATOR_NOT_REGISTERED_ANYWHERE"
    assert not result.unclassified


def test_probe_env_haystack_stays_green(tmp_path):
    """An assertion about the process environment is never a withhold, registered or not."""
    _write_probe(tmp_path, "", "COORDINATOR_NOT_REGISTERED_ANYWHERE", "os.environ")
    findings = scan_dir_for_findings(tmp_path, scan_dirs=("coordinator_core",))
    result = run_census(findings, registered_knobs(), waivers={})
    assert not result.unregistered
    assert not result.unclassified
    assert len(result.not_withheld) == 1


def test_probe_waived_assertion_is_green_and_unwaiving_turns_it_red(tmp_path):
    """A waived assertion is green; removing its waiver-table entry turns it red — proving
    the waiver is load-bearing rather than decorative."""
    _write_probe(tmp_path, 'reason = "refusal text"', "COORDINATOR_PROBE_WAIVED_KNOB", "reason")
    findings = scan_dir_for_findings(tmp_path, scan_dirs=("coordinator_core",))
    waived_key = (findings[0].relpath, findings[0].knob)

    waived_result = run_census(findings, registered_knobs(), waivers={waived_key: "probe"})
    assert not waived_result.unregistered

    unwaived_result = run_census(findings, registered_knobs(), waivers={})
    assert len(unwaived_result.unregistered) == 1


def test_probe_line_insertion_above_a_waiver_does_not_unwaive_it(tmp_path):
    """DEFECT 1 regression pin: the waiver table is keyed by (relpath, knob), not
    (relpath, lineno). Inserting lines above a waived assertion must not turn it red —
    proving the fix rather than merely asserting the new key shape in isolation."""
    _write_probe(tmp_path, 'reason = "refusal text"', "COORDINATOR_PROBE_WAIVED_KNOB", "reason")
    findings = scan_dir_for_findings(tmp_path, scan_dirs=("coordinator_core",))
    waived_key = (findings[0].relpath, findings[0].knob)
    original_lineno = findings[0].lineno

    probe_path = tmp_path / findings[0].relpath
    original_text = probe_path.read_text(encoding="utf-8")
    shifted_text = ("# an unrelated comment\n" * 5) + original_text
    probe_path.write_text(shifted_text, encoding="utf-8")

    shifted_findings = scan_dir_for_findings(tmp_path, scan_dirs=("coordinator_core",))
    assert shifted_findings[0].lineno == original_lineno + 5
    assert shifted_findings[0].knob == findings[0].knob

    result = run_census(shifted_findings, registered_knobs(), waivers={waived_key: "probe"})
    assert not result.unregistered
    assert len(result.waived) == 1


def test_probe_unrecognised_haystack_is_reported_not_ignored(tmp_path):
    """A haystack the classifier has never seen must fail as unclassified, not decay to
    silent 'ignore' — the failure mode that kills this class of guard for good."""
    _write_probe(
        tmp_path,
        'some_brand_new_haystack_shape = "refusal text"',
        "COORDINATOR_PROBE_KNOB",
        "some_brand_new_haystack_shape",
    )
    findings = scan_dir_for_findings(tmp_path, scan_dirs=("coordinator_core",))
    result = run_census(findings, registered_knobs(), waivers={})
    assert len(result.unclassified) == 1
    assert not result.unregistered  # unclassified, not silently folded into either bucket


# ---------------------------------------------------------------------------
# Classifier unit tests — pin the discriminator directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "haystack",
    ["err", "msg", "stderr", "reason", "note", "text", "ctx", "result", "flag_note", "c1_ctx"],
)
def test_classifier_recognises_agent_facing_haystacks(haystack):
    assert classify_haystack(haystack) == WITHHOLD


@pytest.mark.parametrize(
    "haystack",
    ["os.environ", "env", "base_env", "hooks_text", "code", 'captured["env"]', "some_thing_env"],
)
def test_classifier_recognises_non_withhold_haystacks(haystack):
    assert classify_haystack(haystack) == NOT_WITHHOLD


def test_classifier_reports_unknown_haystacks_as_unclassified():
    assert classify_haystack("some_brand_new_shape") == UNCLASSIFIED
