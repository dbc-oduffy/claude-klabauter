"""coordinator_core.ops.docgen.tests.test_dr_write_site_guard — write-site
collision-check tripwire for the ``doc.scaffold`` op.

Purpose: ``dr_allocator.py``'s module docstring makes a promise —
"A future ``doc.scaffold`` op wiring this in front of an actual write MUST
re-add that check at the write site" — referring to the oracle's
``_assert_dr_id_unique`` write-time collision guard
(``coordinator/bin/coordinator-doc-new.py`` in the DoE clone), which this port
deliberately does not carry (allocate-without-write has no collision window
to guard yet).

Why this exists: ``DR-HOLODECK-006`` was allocated on 2026-06-15 and
collided eight ways before the collision was discovered on 2026-07-20 — five
silent weeks, with the duplicate id leaked into shipped C++ strings before
anyone noticed. The defect shape was exactly "allocate, then write, with
nothing re-checking uniqueness at the write site." A docstring promise to
"re-add that check" is not a check; it is prose that a future implementer can
miss precisely the way DR-HOLODECK-006's original author missed it. This test
converts the promise into something that actively fails once the write site
exists and is unguarded, instead of trusting a comment to be re-read.

Design — conditional tripwire, not ``xfail`` (negative-spec):
    An ``@pytest.mark.xfail`` on "doc.scaffold doesn't check uniqueness yet"
    would sit permanently xfailed and never fire once the op landed unguarded
    — it would reproduce the exact silent-promise failure mode this test
    exists to prevent, just wearing test clothing. Instead:
      - While ``doc.scaffold`` is absent, this test SKIPS (inert, but loud
        about why — the skip reason names the guard so a reader grepping the
        suite for "doc.scaffold" or "collision" finds an armed tripwire, not
        a forgotten one).
      - The moment a ``doc.scaffold`` write site is registered, this test
        stops skipping and asserts the write site re-verifies the allocated
        id is not already on disk immediately before writing. An unguarded
        write site fails loudly, at collection/run time, in CI/local test
        runs — not five weeks later in a leaked artifact.

Spec backlink: pln-strang-12-document-generation--75a7eb § C6 (AC5), D1
Originating memo: cross-repo/inbox/2026-07-22-claude-central-em-dr-allocator-consume-not-reimplement.md
Reference implementation: DoE clone `coordinator/bin/coordinator-doc-new.py` `_assert_dr_id_unique`
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

_DOC_SCAFFOLD_OP_NAME = "doc.scaffold"

# NOT just the docgen/ subpackage: every existing scaffold op in this repo
# (workflow_scaffold.py -> "workflow.scaffold", new_project_scaffold.py)
# lives directly in coordinator_core/ops/, one level ABOVE docgen/, not
# inside it. doc.scaffold's overwhelmingly likely landing site is a sibling
# module such as coordinator_core/ops/doc_scaffold.py. A probe scoped to
# docgen/ alone would skip forever while an unguarded write site sat right
# next door — precisely DR-HOLODECK-006's shape: a check everyone believes
# is armed that structurally cannot fire. So the scan root is the whole
# coordinator_core/ops/ tree, derived from docgen's own parent directory
# rather than hardcoded.
_OPS_ROOT = Path(__file__).resolve().parent.parent.parent
_OPS_PACKAGE = "coordinator_core.ops"

# This test's own module path, so the probe excludes its own source from
# the scan. Self-exclusion is load-bearing, not cosmetic: this module's
# docstrings quote the literal `register_op("doc.scaffold"` needle as prose
# example text, so an unfiltered recursive scan would match this file and
# report itself as the write site.
_SELF_PATH = Path(__file__).resolve()

# Keywords a write-site's source is expected to contain if it re-verifies
# uniqueness immediately before writing (mirrors the oracle's
# `_assert_dr_id_unique` shape: re-scan/re-check the target namespace for the
# allocated id, then raise/refuse on collision rather than clobber).
_UNIQUENESS_CHECK_MARKERS = (
    "assert_dr_id_unique",
    "assert_unique",
    "_assert_dr_id_unique",
    "collision",
    "already exists",
    "already allocated",
    "FileExistsError",
)


def _find_doc_scaffold_registration(search_root: Path | None = None) -> str | None:
    """Return the dotted module name registering ``doc.scaffold``, or None.

    Probes source text (not live-registry state) so this works whether or
    not the module has been imported yet, and does not require guessing an
    eager-import order. Recursively scans every non-test, non-``__init__.py``
    module under ``search_root`` (default: the whole ``coordinator_core/ops/``
    tree, NOT just the ``docgen/`` subpackage — see the module-level comment
    on ``_OPS_ROOT`` for why the wider root is load-bearing) for a
    ``register_op("doc.scaffold"`` (or single-quote variant) call, the same
    registration convention used elsewhere in ``coordinator_core/ops`` (e.g.
    ``workflow_scaffold.py``'s ``@register_op("workflow.scaffold")``).

    ``search_root`` is an injectable parameter (default ``None`` -> the real
    ``_OPS_ROOT``) so this helper is unit-testable against a scratch
    directory without touching the live tree — see
    ``test_probe_finds_registration_in_scratch_tree`` /
    ``test_probe_returns_none_when_absent`` below.
    """
    root = search_root if search_root is not None else _OPS_ROOT
    package = _OPS_PACKAGE if search_root is None else None
    needles = (
        f'register_op("{_DOC_SCAFFOLD_OP_NAME}"',
        f"register_op('{_DOC_SCAFFOLD_OP_NAME}'",
    )
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("test_"):
            continue
        if path.resolve() == _SELF_PATH:
            continue
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in needles):
            rel = path.resolve().relative_to(root.resolve())
            dotted = ".".join(rel.with_suffix("").parts)
            return f"{package}.{dotted}" if package else dotted
    return None


def test_doc_scaffold_write_site_reverifies_uniqueness_before_writing():
    """The moment doc.scaffold exists, its write site must re-check uniqueness.

    Guard name: dr-write-site-collision-guard. If you are grepping for why
    this test exists: it is the enforcing counterpart to dr_allocator.py's
    docstring promise, armed against a repeat of DR-HOLODECK-006 (allocated
    2026-06-15, collided eight ways, discovered 2026-07-20 after five silent
    weeks in shipped C++ strings).
    """
    module_name = _find_doc_scaffold_registration()
    if module_name is None:
        pytest.skip(
            "dr-write-site-collision-guard: doc.scaffold write site not yet "
            "implemented — guard inert, will arm automatically once "
            "doc.scaffold is registered anywhere under coordinator_core/ops/"
        )

    module = importlib.import_module(module_name)
    module_source = inspect.getsource(module)

    has_marker = any(marker in module_source for marker in _UNIQUENESS_CHECK_MARKERS)

    assert has_marker, (
        "dr-write-site-collision-guard FIRED: "
        f"{module_name} registers doc.scaffold but its source contains none "
        f"of the expected write-time uniqueness-check markers "
        f"{_UNIQUENESS_CHECK_MARKERS!r}. This is the exact defect shape that "
        "produced DR-HOLODECK-006 (allocated 2026-06-15, collided eight "
        "ways, discovered 2026-07-20 after five silent weeks, leaked into "
        "shipped C++ strings): an id is allocated via "
        "dr_allocator.allocate_dr_number() and then written to disk with "
        "nothing re-verifying, at write time, that the allocated id is "
        "still unique. Re-add the oracle's write-time collision check "
        "before writing — reference implementation: "
        "coordinator/bin/coordinator-doc-new.py `_assert_dr_id_unique` "
        "(DoE clone). See coordinator_core/ops/docgen/dr_allocator.py's "
        "module docstring § Scope for the promise this test enforces."
    )


# --- Probe self-tests -------------------------------------------------------
#
# The tripwire above is unfalsifiable if the probe it depends on has never
# been proven to actually find a registration — "trust the probe" is the
# same species of promise as "trust the docstring" this whole file exists to
# replace. These two tests exercise `_find_doc_scaffold_registration()`
# directly against scratch trees, independent of whatever doc.scaffold's
# real landing site turns out to be.


def test_probe_finds_registration_in_scratch_tree(tmp_path):
    (tmp_path / "doc_scaffold.py").write_text(
        'from coordinator_core.ipc import register_op\n\n'
        '@register_op("doc.scaffold")\n'
        'async def _doc_scaffold(params, repo_root=None):\n'
        '    ...\n',
        encoding="utf-8",
    )
    assert _find_doc_scaffold_registration(search_root=tmp_path) == "doc_scaffold"


def test_probe_finds_registration_nested_under_scratch_tree(tmp_path):
    nested = tmp_path / "docgen"
    nested.mkdir()
    (nested / "scaffold.py").write_text(
        "register_op('doc.scaffold', _handler)\n", encoding="utf-8"
    )
    assert _find_doc_scaffold_registration(search_root=tmp_path) == "docgen.scaffold"


def test_probe_returns_none_when_absent(tmp_path):
    (tmp_path / "unrelated.py").write_text(
        'register_op("workflow.scaffold")\n', encoding="utf-8"
    )
    (tmp_path / "test_doc_scaffold.py").write_text(
        'register_op("doc.scaffold")\n', encoding="utf-8"
    )
    assert _find_doc_scaffold_registration(search_root=tmp_path) is None


def test_probe_ignores_init_py(tmp_path):
    (tmp_path / "__init__.py").write_text(
        'register_op("doc.scaffold")\n', encoding="utf-8"
    )
    assert _find_doc_scaffold_registration(search_root=tmp_path) is None
