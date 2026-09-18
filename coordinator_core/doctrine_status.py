"""coordinator_core/doctrine_status.py -- read a doctrine page's `status:`
frontmatter value and check it against the closed vocabulary contract.

Purpose: `<repo>/coordinator/contract/doctrine-status-vocabulary.json` closes the
`status:` field at a fixed vocabulary (`active`, `deprecated`, `distilled`,
`shipped`, `stub` in DoE-claude's own governance corpus). Nothing previously checked a
page's value against that contract -- a ninth spelling could land tomorrow
and nothing would notice. This module is the read-and-check half; the CLI
at `coordinator/bin/check-doctrine-status.py` is the exit-code half.

`load_vocabulary` raises `ValueError` for a contract that parses as JSON
but is shaped wrong (missing `vocabulary` key, or a non-list-of-
strings value) -- the CLI routes this, alongside `json.JSONDecodeError`, to its
exit-2 "unusable contract" path.

THREE PAGE CLASSES ARE SILENT, BY DESIGN. Absence of frontmatter, and
frontmatter without a `status:` key, are never violations -- this is the
frontmatter-backfill rejection (DoE-claude
`docs/plans/2026-08-30-doctrine-governance-tier-2.md` Anti-scope), enforced
mechanically here rather than left as a convention a future editor could
violate by accident. Only a page that HAS a `status:` key with a value
outside the contract's vocabulary is a violation.

Frontmatter parsing reuses `coordinator_core.agent_frontmatter.split_frontmatter`
via an ordinary package import -- this module and its sibling both now live
inside the engine package, so the by-file-location `importlib` load
DoE-claude's own copy needed (its `coordinator/` tree carries no
`__init__.py`) no longer applies here.

PATH RESOLUTION -- SESSION REPO CLASS. `WIKI_ROOT`/`CONTRACT_PATH` no longer derive
from this module's own `__file__` (moving the module into the engine would otherwise
point them at the ENGINE's checkout, never the repo whose doctrine corpus is under
test) -- both resolve from the CALLER's repo root, walked up from cwd via
`coordinator_core.git.repo_root.show_toplevel`
(`docs/plans/2026-09-18-doe-holds-no-scripts.md` § Path resolution). A caller with no
resolvable repo root falls back to the process cwd itself, so these stay real `Path`
objects rather than `None` -- an unusable root then surfaces through the CLI's
ordinary `is_dir()`/`is_file()` checks (exit 2), not an import-time crash.

Negative-spec: no CLI, no output formatting, no exit codes, no directory
walk caching -- pure functions over paths/text returning structured
results, so a caller (the CLI, or a test) can assert on data. No staleness,
no git history, no second unrelated concern sharing this module -- the
earlier DoE-claude draft's `doctrine_governance.py` bundled two unrelated
things and needed a third chunk to reunite them; this module is
deliberately narrow.

Spec backlink: DoE-claude docs/plans/2026-08-30-doctrine-governance-tier-2.md, chunk C2.
docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W3-C2.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from coordinator_core.agent_frontmatter import split_frontmatter
from coordinator_core.git.repo_root import show_toplevel as _show_toplevel


def _default_repo_root() -> Path:
    resolved = _show_toplevel()
    return Path(resolved) if resolved else Path.cwd()


REPO_ROOT = _default_repo_root()
CONTRACT_PATH = REPO_ROOT / "coordinator" / "contract" / "doctrine-status-vocabulary.json"
WIKI_ROOT = REPO_ROOT / "coordinator" / "docs" / "wiki"

STATUS_LINE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class StatusFinding:
    """One page's `status:` verdict.

    `has_status` is False for both silent classes (no frontmatter, and
    frontmatter without a `status:` key) -- callers branch on `has_status`
    first and never inspect `value` when it is False.
    """

    path: Path
    has_status: bool
    value: "str | None"
    is_violation: bool


def load_vocabulary(contract_path: Path = CONTRACT_PATH) -> tuple[str, ...]:
    """The closed vocabulary from the contract file, as an ordered tuple.

    Raises `FileNotFoundError`/`json.JSONDecodeError` unmodified on a
    missing or malformed contract -- a caller with an unusable contract
    cannot silently treat every value as valid. Also raises `ValueError`
    if the contract parses as JSON but its shape is unusable: no
    `vocabulary` key, or a `vocabulary` value that isn't a list of
    strings.

    Review: code-reviewer -- a contract that is valid JSON but has no
    `vocabulary` key (or has it as a non-list) previously raised
    `KeyError`/`TypeError` past the CLI's `except json.JSONDecodeError`,
    producing exit 1 (an uncaught traceback) instead of the documented
    exit 2 for an unusable contract.
    """
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    if "vocabulary" not in data:
        raise ValueError(f"contract at {contract_path} has no \"vocabulary\" key")
    vocabulary = data["vocabulary"]
    if not isinstance(vocabulary, list) or not all(isinstance(v, str) for v in vocabulary):
        raise ValueError(
            f"contract at {contract_path} has a \"vocabulary\" value that is not "
            "a list of strings"
        )
    return tuple(vocabulary)


def extract_status(text: str) -> "str | None":
    """The frontmatter `status:` value, or `None` if the page has no
    frontmatter or has frontmatter without a `status:` key.

    Bounded to the frontmatter block between the leading `---` fences --
    a naive whole-file `status:` search over-reports because body prose
    (a code block, a sentence describing the field itself) can also match.
    """
    frontmatter, _body = split_frontmatter(text)
    if not frontmatter:
        return None
    match = STATUS_LINE.search(frontmatter)
    if match is None:
        return None
    return match.group(1).strip().strip("'\"")


def check_page(path: Path, vocabulary: Iterable[str]) -> StatusFinding:
    """Read one page and classify it against `vocabulary`.

    Never raises on the two silent classes; a page this function cannot
    read (permissions, encoding) is the caller's problem to surface, not
    one this function papers over -- read errors propagate. The CLI
    catches `UnicodeDecodeError` specifically and routes it to exit 2
    naming the unreadable file, so it never collides with exit 1's
    "unlisted status value" contract.
    """
    text = path.read_text(encoding="utf-8")
    value = extract_status(text)
    if value is None:
        return StatusFinding(path=path, has_status=False, value=None, is_violation=False)
    vocab = tuple(vocabulary)
    return StatusFinding(
        path=path,
        has_status=True,
        value=value,
        is_violation=value not in vocab,
    )


def iter_wiki_pages(wiki_root: Path = WIKI_ROOT) -> "list[Path]":
    """Every `.md` file under `wiki_root`, sorted for deterministic output."""
    return sorted(wiki_root.rglob("*.md"))
