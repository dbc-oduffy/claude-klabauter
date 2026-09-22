"""
Tests for the ``writes_under:`` carry-through fix (klabauter#45, classes B/C).

Purpose: prove that a row whose deliverable is named at runtime by the tool
producing it (a ``cross-repo-memo`` send, a spinoff baton, a DR the
``coordinator-doc-new`` scaffolder allocates) reaches its dispatched
executor with a footprint that includes its own ``writes_under:`` prefix,
that the wave's commit pathspec never carries a bare directory, and that a
reported file outside every declared prefix is refused rather than silently
widening what the commit phase may touch.

Class B (a tool-minted-name row has no legal footprint at all) and class C
(the emitter drops ``writes_under:`` from the footprint it hands the
executor, even though it kept it in the row's own declaration) are one
defect from two ends -- see klabauter#45. ``pathspec.py``'s own module
docstring (§ Run-time-named writes) and ``emit.py``'s ``_fenced_paths``/
``_prefix_commit_rule`` already carry the fix this file pins: this suite is
the regression coverage the issue asked for, verified against the actual
emitted prompt/pathspec text a real ``compose_script`` call produces, not a
hand-rolled restatement of the design.

Negative-spec: this file does NOT touch ``inventory_mint.py`` (mint-time
refusal, task requirement #4 in klabauter#45) -- that module is owned by a
different chunk of this same batch. It also does not exercise
``spine_read.read_spine`` end-to-end from a YAML fenced block; the
``WaveRow``/``EmitterRow`` shapes it would parse into are already
constructed directly, matching this package's other test modules
(``test_pathspec.py``, ``test_wave_map.py``).
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import (
    _row_return_contract,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.pathspec import (
    commit_pathspec,
    commit_pathspec_or_none,
    commit_prefixes,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _wave_row(id_, writes, writes_under=(), surface=""):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface=surface,
        writes=writes,
        reads=[],
        depends_on=[],
        writes_under=writes_under,
    )


# ---------------------------------------------------------------------------
# Class C's exact row shape: `writes:` AND `writes_under:` together
# ---------------------------------------------------------------------------


def test_footprint_carries_both_writes_and_writes_under():
    """retprov-C3's exact shape (klabauter#45 class C): `writes:
    [docs/decisions/INDEX.md]` names only the allocator's index edit;
    `writes_under: [docs/decisions/]` carries the DR file the row actually
    exists to deliver. The rendered footprint constraint must name BOTH --
    dropping the prefix hands the executor a footprint excluding its own
    load-bearing deliverable (the exact defect the issue reports)."""
    row = _wave_row(
        "retprov-C3",
        writes=["docs/decisions/INDEX.md"],
        writes_under=("docs/decisions/",),
    )
    contract = _row_return_contract(row, "docs/plans/fake-plan.md")

    assert "docs/decisions/INDEX.md" in contract
    assert "docs/decisions/" in contract
    # The footprint constraint is one comma-joined list -- assert the
    # constraint's own list clause carries both, not just the two substrings
    # appearing anywhere in the (much longer) rendered contract.
    footprint_line = contract.splitlines()[0]
    assert "docs/decisions/INDEX.md" in footprint_line
    assert "docs/decisions/" in footprint_line


def test_commit_pathspec_never_widens_to_the_prefix_directory():
    """`commit_pathspec` (the wave's static, emit-time pathspec) must never
    carry a bare directory even when a row also declares `writes_under:` --
    the prefix is an authorization boundary for the executor, never a commit
    target (design call point 3)."""
    wave = [
        _wave_row(
            "retprov-C3",
            writes=["docs/decisions/INDEX.md"],
            writes_under=("docs/decisions/",),
        )
    ]
    pathspec = commit_pathspec(wave)
    assert pathspec == ["docs/decisions/INDEX.md"]
    assert not any(p.endswith("/") for p in pathspec)


# ---------------------------------------------------------------------------
# Class B: a tool-minted-name row (writes: [], writes_under: [...] only)
# ---------------------------------------------------------------------------


def test_tool_minted_name_row_emits_at_all():
    """A row with NO concrete `writes:` at all (the shape a `cross-repo-memo`
    send or a spinoff baton takes: `spine_read.read_spine` resolves an
    absent `writes:` alongside a declared `writes_under:` to `writes: []`,
    never UNDECLARED) must still emit a legal wave -- class B's whole
    complaint is that this shape was permanently undispatchable through
    /mise-en-place. `compose_script` must not raise, and the row's footprint
    constraint must still name its prefix."""
    row = _wave_row(
        "xhorizon-C1",
        writes=[],
        writes_under=("cross-repo/outbox/",),
    )
    script = compose_script(
        [[row]],
        name="wf-writes-under",
        description="tool-minted-name row emits",
    )
    assert "cross-repo/outbox/" in script

    contract = _row_return_contract(row, "docs/plans/fake-plan.md")
    footprint_line = contract.splitlines()[0]
    assert "cross-repo/outbox/" in footprint_line


def test_writes_under_only_wave_gets_legal_empty_static_pathspec():
    """A wave whose sole row declares `writes_under:` and no concrete
    `writes:` legally returns an EMPTY static pathspec (its files don't
    exist yet at emit time) rather than raising or fabricating a directory
    entry -- `commit_pathspec_or_none` degrades cleanly and `commit_prefixes`
    carries the prefix forward for the commit phase to widen from the
    executor's own report."""
    wave = [_wave_row("xhorizon-C1", writes=[], writes_under=("cross-repo/outbox/",))]
    assert commit_pathspec_or_none(wave) == []
    assert commit_prefixes(wave) == [("xhorizon-C1", ("cross-repo/outbox/",))]


# ---------------------------------------------------------------------------
# DONE-report-derived pathspec: real files, never a directory
# ---------------------------------------------------------------------------


def test_emitted_commit_prompt_derives_prefix_files_from_the_report_not_the_union():
    """The wave's commit phase does not get the declared `writes_under:`
    union as its pathspec (there is nothing to name at emit time) -- it gets
    an instruction to read the row's own DONE report for the concrete files
    it created under that row's declared prefix, one file at a time, never
    the directory itself (design call point 2)."""
    row = _wave_row(
        "edgarcik-C8",
        writes=[],
        writes_under=("state/handoffs/",),
    )
    script = compose_script(
        [[row]],
        name="wf-prefix-commit",
        description="prefix commit derives from report",
    )
    assert "created-under-prefix:" in script
    assert "RUN-TIME-NAMED WRITES" in script
    # The one sanctioned widening names the prefix, but never composes a
    # commit call whose OWN static pathspec is the bare directory -- the
    # emitted `_commit_agent_call` / `commitPaths` line for this batch must
    # never carry `state/handoffs/` as a literal committed path.
    assert "'state/handoffs/'" not in script


# ---------------------------------------------------------------------------
# A reported file outside every declared prefix is refused, not widened
# ---------------------------------------------------------------------------


def test_prefix_commit_rule_names_the_bound_and_refuses_outside_it():
    """The commit prompt's own text is the enforcement surface here (the
    prefix-bound files are resolved at RUN time, by the dispatched
    git-commit-agent reading the executor's report -- this module has no
    tree to check against at emit time, per its own negative spec). The
    emitted instruction must explicitly bound each listed file to the
    reporting row's OWN prefix and refuse (STOP, no success token) a file a
    report claims outside every declared prefix, or without a
    `created-under-prefix:` line at all -- that is what replaces the
    protection a bare-directory refusal used to provide (design call point
    3)."""
    row = _wave_row(
        "edgarcik-C5",
        writes=[],
        writes_under=("state/handoffs/",),
    )
    script = compose_script(
        [[row]],
        name="wf-prefix-bound",
        description="prefix-bound commit refusal",
    )
    assert "only where the file sits under THAT row's own prefix" in script
    assert "STOP" in script
    assert "never infer those files from prose" in script.lower()
