"""
Zero-spawn fast-tier drift leg (C4a, AC6b) for coordinator_core.docindex.

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C4a

WHAT THIS ASSERTS (AC6b): for every document carrying `index_source_dir:` in
its own frontmatter, the bytes of its emitted GENERATED-docindex region hash
to the sha256 digest recorded in that same document's own closing sentinel.
This is a hand-edit detector (AC12) and nothing more: no directory read, no
entry file read, no git call, no dependency on any peer's concurrent edit —
which is exactly why it is safe to run on the fast tier under the
50-70-concurrent-LLM load norm.

Zero self-declared indexes on disk is a PASS (AC7) — that is the state at
HEAD until C5 converts `docs/architecture/systems-index.md`; this test is
green from the moment it is written and gains its real subject once C5 lands.

Negative-spec:
  - Discovery never spawns `git` (or any subprocess) — the repo root is
    resolved statically via `Path(__file__).resolve().parents[N]`, matching
    the sibling convention in `coordinator_core/frontmatter/tests/`.
  - Discovery parses each candidate's leading frontmatter block for
    `index_source_dir:` only — never a text/grep scan across file contents.
  - Does not read the index's declared source directory, does not read any
    entry file, and does not consult git or HEAD in any way — C4b (gated by
    E1) is the leg that does that.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.docindex.compare import CompareError, extract_region, region_digest
from coordinator_core.frontmatter.primitives import split_frontmatter

# coordinator_core/docindex/tests/test_index_digest.py -> parents[3] is the repo root
# (parents[0]=tests, [1]=docindex, [2]=coordinator_core, [3]=repo root), matching
# coordinator_core/frontmatter/tests/test_roadmap_approval_identity.py's convention.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "state",
        "archive",
        "tasks",
    }
)


def _discover_self_declaring_index_docs() -> list[Path]:
    """Walk the repo tree statically (no git spawn) and return every markdown
    document whose own leading frontmatter block declares `index_source_dir:`.

    Frontmatter-parsed discovery only — never a text/grep scan — matching
    C4a's DISCOVERY WITHOUT GIT paragraph and C3's exclusion convention.
    """
    found: list[Path] = []
    stack = [_REPO_ROOT]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in _SKIP_DIR_NAMES:
                    continue
                stack.append(entry)
                continue
            if entry.suffix != ".md":
                continue
            try:
                text = entry.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            split = split_frontmatter(text)
            if split is None:
                continue
            try:
                fm = yaml.safe_load(split.fm_text)
            except yaml.YAMLError:
                continue
            if not isinstance(fm, dict):
                continue
            if not isinstance(fm.get("index_source_dir"), str):
                # A top-level key check, not a substring scan — a document
                # merely *mentioning* `index_source_dir:` in prose (e.g. this
                # plan's own `prime_exit_criterion` field) is not a
                # self-declaring index and must not be discovered as one.
                continue
            found.append(entry)
    return found


def test_every_self_declaring_index_region_matches_its_own_recorded_digest():
    """AC6b: for every discovered self-declaring index document, the on-disk
    region's sha256 digest matches the digest recorded in its own closing
    sentinel. Zero discovered documents is a PASS (AC7)."""
    docs = _discover_self_declaring_index_docs()

    for doc_path in docs:
        text = doc_path.read_text(encoding="utf-8")
        try:
            region_text, recorded_digest = extract_region(text)
        except CompareError as e:
            raise AssertionError(
                f"{doc_path}: declares index_source_dir: but carries no "
                f"recognizable GENERATED-docindex region: {e}"
            ) from e
        actual_digest = region_digest(region_text.encode("utf-8"))
        assert actual_digest == recorded_digest, (
            f"{doc_path}: on-disk region digest {actual_digest} disagrees with "
            f"its own recorded closing-sentinel digest {recorded_digest} — "
            f"hand-edit inside the delimited region."
        )


def test_discovery_finds_no_self_declaring_indexes_at_head_pre_c5():
    """AC7 sentinel: documents this leg's own starting state so its subject
    (a converted systems-index.md) is visible the moment C5 lands. If this
    starts failing, C5 has landed and the assertion above is this test's
    real subject now."""
    docs = _discover_self_declaring_index_docs()
    assert docs == [] or all(
        doc.name == "systems-index.md" for doc in docs
    ), (
        "unexpected self-declaring index document(s) discovered before C5: "
        f"{[str(d) for d in docs]}"
    )
