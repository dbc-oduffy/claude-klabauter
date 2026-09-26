
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


def test_footprint_carries_both_writes_and_writes_under():
    row = _wave_row(
        "retprov-C3",
        writes=["docs/decisions/INDEX.md"],
        writes_under=("docs/decisions/",),
    )
    contract = _row_return_contract(row, "docs/plans/fake-plan.md")

    assert "docs/decisions/INDEX.md" in contract
    assert "docs/decisions/" in contract
    footprint_line = contract.splitlines()[0]
    assert "docs/decisions/INDEX.md" in footprint_line
    assert "docs/decisions/" in footprint_line


def test_commit_pathspec_never_widens_to_the_prefix_directory():
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
    wave = [_wave_row("xhorizon-C1", writes=[], writes_under=("cross-repo/outbox/",))]
    assert commit_pathspec_or_none(wave) == []
    assert commit_prefixes(wave) == [("xhorizon-C1", ("cross-repo/outbox/",))]


def test_emitted_commit_prompt_derives_prefix_files_from_the_report_not_the_union():
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
    assert "'state/handoffs/'" not in script


def test_prefix_commit_rule_names_the_bound_and_refuses_outside_it():
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
