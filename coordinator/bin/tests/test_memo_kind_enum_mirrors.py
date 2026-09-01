"""The memo-kind enum is mirrored in two CLI scripts; this pins them to the engine.

`coordinator_core.ops.fleet._memo_compose._VALID_KINDS` is the canonical list.
Three engine-side readers import it directly (`schema_validate._memo_cf_kind_enum`,
`_outbox_frontmatter_rules.VALID_KINDS`, `contract.emit_memo_schema`), so they
cannot drift. Two CLI scripts cannot import it without paying its transitive
chain (session.core, ops.ceremony.git_native, git.commit_trailers, dag,
lifecycle) on every interpreter start, and both are on hot paths -- so they hold
literal tuples and this test is what keeps those honest.

Why this test exists rather than a sixth import: `bug` was added to the engine
list and to `cross-repo-memo._VALID_KINDS`, but not to
`coordinator-doc-new._MEMO_KINDS`. The result was a kind the sender accepted and
the scaffolder refused, in different words, with nothing failing anywhere.

Negative-spec: this pins SET EQUALITY, not order. Order is presentation --
each site joins its own tuple into its own message and no consumer parses one
site's ordering against another's.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._memo_compose import _VALID_KINDS as CANONICAL

_BIN = Path(__file__).resolve().parents[1]


def _load(script_name: str, module_name: str):
    """Load a hyphenated bin script as a module (it is not importable by name)."""
    path = _BIN / script_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        pytest.skip(f"{script_name} not loadable as a module")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "script_name,module_name,attr",
    [
        ("coordinator-doc-new.py", "_pin_doc_new", "_MEMO_KINDS"),
        ("cross-repo-memo.py", "_pin_cross_repo_memo", "_VALID_KINDS"),
    ],
)
def test_cli_memo_kind_mirror_matches_the_engine(script_name, module_name, attr):
    mod = _load(script_name, module_name)
    mirrored = getattr(mod, attr)
    assert set(mirrored) == set(CANONICAL), (
        f"{script_name}::{attr} is {sorted(mirrored)} but the engine's "
        f"_memo_compose._VALID_KINDS is {sorted(CANONICAL)}. "
        f"Missing here: {sorted(set(CANONICAL) - set(mirrored))}; "
        f"extra here: {sorted(set(mirrored) - set(CANONICAL))}. "
        "Add the value to the literal tuple in that script -- do NOT import "
        "_memo_compose there; see this module's own docstring for why."
    )


def test_ack_is_not_a_kind_anywhere():
    """`ack` is receipt-state, not a sender-declared kind -- asserted at the
    canonical list so the mirrors inherit it."""
    assert "ack" not in CANONICAL
