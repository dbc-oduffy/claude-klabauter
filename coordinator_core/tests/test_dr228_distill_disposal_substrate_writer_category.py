"""coordinator_core.tests.test_dr228_distill_disposal_substrate_writer_category
— existence + citation guard for DR-228.

Purpose: DR-228 (docs/decisions/DR-228-distill-disposal-substrate-writer-category.md)
is the governing decision-of-record admitting six new ops — the distill-disposal
tier (``distill.assemble_disposal_manifest``, ``distill.stamp_disposal``,
``distill.apply_disposal``) and the distill-scratch tier (``distill.scope``,
``distill.curation_status``, ``memo.fate_partition``) — as a sanctioned MUTATING
substrate-writer category crossing ``coordinator_core/ipc.py``'s read-mostly
negative-spec. Per DR-208 § Classification-Correctness Discipline, every one of
these six ops needs a per-op ``OP_CLASSIFICATION`` entry in
``coordinator_core/authz/classification.py`` citing this DR — a silent entry with
no DR citation is exactly the drift the discipline exists to prevent.

Design — conditional tripwire, not a hard requirement at DR-authoring time
(mirrors ``coordinator_core/ops/docgen/tests/test_dr_write_site_guard.py``'s
established shape for the identical problem: a governing decision lands before
its consuming implementations, in a separate, concurrently-dispatched chunk of
the same plan wave):
    - This test's first half is unconditional: the DR file must exist on disk,
      with a well-formed ``id: DR-228`` frontmatter line, and its body must name
      all six ops it admits — this is checkable the moment the DR itself lands
      and never skips.
    - This test's second half (classification-citation checks) SKIPS per op
      while that op has no ``OP_CLASSIFICATION`` entry yet — inert, but loud
      about why, exactly as the doc.scaffold guard does for an absent write
      site. The moment each op's classification.py entry lands (C10-C14/C16,
      dispatched independently), this test stops skipping for that op and
      asserts the entry's surrounding source cites "DR-228" — an op landing
      without the citation fails loudly here instead of silently drifting from
      the DR's own admission.

Spec backlink: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C9b
Governing decision: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md
Reference implementation shape: coordinator_core/ops/docgen/tests/test_dr_write_site_guard.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DR_PATH = _REPO_ROOT / "docs" / "decisions" / "DR-228-distill-disposal-substrate-writer-category.md"
_CLASSIFICATION_PATH = _REPO_ROOT / "coordinator_core" / "authz" / "classification.py"
_IPC_PATH = _REPO_ROOT / "coordinator_core" / "ipc.py"

# The six ops DR-228 admits, split by tier (see DR-228 § D1 / § D6).
_DISPOSAL_TIER_OPS = (
    "distill.assemble_disposal_manifest",
    "distill.stamp_disposal",
    "distill.apply_disposal",
)
_SCRATCH_TIER_OPS = (
    "distill.scope",
    "distill.curation_status",
    "memo.fate_partition",
)
_ALL_SIX_OPS = _DISPOSAL_TIER_OPS + _SCRATCH_TIER_OPS

_DR_CITATION_MARKERS = ("DR-228",)


def test_dr228_file_exists_with_well_formed_frontmatter():
    """DR-228 must exist on disk with a matching ``id:`` frontmatter line."""
    assert _DR_PATH.is_file(), (
        f"DR-228 not found at expected path {_DR_PATH} — the governing decision "
        "for the distill-disposal substrate-writer category (plan task C9b) is "
        "missing."
    )
    text = _DR_PATH.read_text(encoding="utf-8")
    frontmatter_match = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert frontmatter_match is not None, "DR-228 file has no YAML frontmatter block"
    assert re.search(r"^id:\s*DR-228\s*$", frontmatter_match.group(1), re.MULTILINE), (
        "DR-228's frontmatter `id:` field does not read `DR-228` — "
        f"frontmatter block was:\n{frontmatter_match.group(1)}"
    )


def test_dr228_names_all_six_admitted_ops():
    """DR-228's body must name every op it admits — the closed sanctioned-ops list."""
    text = _DR_PATH.read_text(encoding="utf-8")
    missing = [op for op in _ALL_SIX_OPS if op not in text]
    assert not missing, (
        "DR-228 does not name the following ops it is supposed to admit: "
        f"{missing!r}. The sanctioned-ops list (§ D1 disposal tier, § D6 scratch "
        "tier) must be a closed, explicit enumeration — an op missing from the "
        "DR text is not covered by it."
    )


@pytest.mark.parametrize("op_name", _ALL_SIX_OPS)
def test_op_classification_entry_cites_dr228(op_name):
    """Guard name: dr228-classification-citation-guard.

    Once ``op_name`` appears in ``classification.py`` (i.e. its
    ``OP_CLASSIFICATION`` entry has landed — a sibling chunk's job, not this
    DR-authoring chunk's), the surrounding source must cite DR-228 by name.
    Until then this test SKIPS per-op, inert but named, so a reader grepping
    for "DR-228" or the op name finds an armed tripwire, not a forgotten one.
    """
    if not _CLASSIFICATION_PATH.is_file():
        pytest.skip(
            "dr228-classification-citation-guard: "
            f"{_CLASSIFICATION_PATH} does not exist yet — guard inert."
        )
    source = _CLASSIFICATION_PATH.read_text(encoding="utf-8")
    quoted = (f'"{op_name}"', f"'{op_name}'")
    if not any(q in source for q in quoted):
        pytest.skip(
            f"dr228-classification-citation-guard: {op_name!r} has no "
            "OP_CLASSIFICATION entry yet — guard inert, will arm automatically "
            "once the op is registered in classification.py."
        )

    # Scope the citation search to the neighbourhood of the op's own entry
    # (a window of source around its first mention), not the whole file — a
    # DR-228 citation anywhere else in the file (e.g. a different op's block)
    # must not satisfy this op's own citation requirement.
    idx = min(
        (source.find(q) for q in quoted if source.find(q) != -1),
        default=-1,
    )
    assert idx != -1
    window = source[max(0, idx - 800) : idx + 800]
    has_citation = any(marker in window for marker in _DR_CITATION_MARKERS)
    assert has_citation, (
        "dr228-classification-citation-guard FIRED: "
        f"{op_name!r} has an OP_CLASSIFICATION entry in {_CLASSIFICATION_PATH} "
        "but the source in its immediate neighbourhood does not cite 'DR-228'. "
        "Per DR-208 § Classification-Correctness Discipline, every MUTATING op "
        "sanctioned under a DR-level carve-out must cite that DR at its "
        "classification entry — see DR-228's own § Impl-slice obligations."
    )


def test_ipc_negative_spec_names_all_six_ops_and_cites_dr228():
    """Guard name: dr228-ipc-negative-spec-guard.

    DR-228 § 3 "Impl-slice obligations" requires, verbatim, a ``coordinator_core/ipc.py``
    negative-spec summary-block entry naming all six ops it admits — the same format
    precedent as the existing FLEET/HANDOFF/QUEUE/FLEET-REAP/MEMO entries in that same
    module docstring. Unlike the per-op classification-citation guard above, this is not
    conditional on a sibling chunk landing first: the DR itself and its ipc.py entry are
    a single-plan-wave completion (DR-228 § 3), so this test does NOT skip.
    """
    assert _IPC_PATH.is_file(), f"{_IPC_PATH} not found"
    source = _IPC_PATH.read_text(encoding="utf-8")

    # Scope the search to ipc.py's own negative-spec docstring block, not the whole file.
    docstring_match = re.search(r'^"""(.*?)^"""', source, re.DOTALL | re.MULTILINE)
    assert docstring_match is not None, "ipc.py has no module docstring to search"
    negative_spec = docstring_match.group(1)

    missing_ops = [op for op in _ALL_SIX_OPS if op not in negative_spec]
    assert not missing_ops, (
        "coordinator_core/ipc.py's negative-spec summary block does not name the "
        f"following DR-228 ops: {missing_ops!r}. DR-228 § 3 'Impl-slice obligations' "
        "requires a dedicated ipc.py entry for all six ops, modeled on the existing "
        "FLEET/HANDOFF/QUEUE/FLEET-REAP/MEMO entries in the same docstring."
    )
    assert "DR-228" in negative_spec, (
        "coordinator_core/ipc.py's negative-spec summary block names the DR-228 ops "
        "but never cites 'DR-228' by name in that block."
    )


# --- Probe self-tests --------------------------------------------------------
#
# The per-op citation check above is unfalsifiable if its own window-scoped
# substring search has never been proven to actually discriminate a cited
# entry from an uncited one. These two tests exercise that logic directly
# against synthetic source text, independent of the real classification.py.


def test_citation_window_logic_detects_present_citation(tmp_path):
    synthetic = (
        "OP_CLASSIFICATION = {}\n"
        "# distill.scope is MUTATING per DR-228 § D6 (scratch-tier writer).\n"
        'OP_CLASSIFICATION["distill.scope"] = OpClass.MUTATING\n'
    )
    idx = synthetic.find('"distill.scope"')
    window = synthetic[max(0, idx - 800) : idx + 800]
    assert "DR-228" in window


def test_citation_window_logic_rejects_absent_citation(tmp_path):
    synthetic = (
        "OP_CLASSIFICATION = {}\n"
        'OP_CLASSIFICATION["distill.scope"] = OpClass.MUTATING\n'
    )
    idx = synthetic.find('"distill.scope"')
    window = synthetic[max(0, idx - 800) : idx + 800]
    assert "DR-228" not in window
