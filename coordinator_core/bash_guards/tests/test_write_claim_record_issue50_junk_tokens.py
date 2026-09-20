"""Regression test for dbc-oduffy/claude-klabauter#50 -- the claim ledger
recorded shell tokens as paths: an unexpanded variable (`$f`), a glued
redirection operator (`2>&1`, `2>/dev/null`), and a bare directory
(`state/mise-inventory`) all reached `touch-record.jsonl` as `VERB_TOUCH`
claims through `write_claim_record.record_write_claims`.

Site: `coordinator_core.bash_guards.write_claim_record._is_claimable_target`
-- the module's own documented seam for rejecting a candidate its shared
extractor (`bump_outside_repo_write._iter_write_sink_candidates`) mis-reads
as a path, without touching that shared extractor (its over-inclusion is
correct for the outside-repo question; this module's negative-spec forbids
"fixing" it there). Drives the real production call path
(`record_write_claims`), not the filter function in isolation, per the
issue's own "verify against the real claiming path" requirement.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards.write_claim_record import record_write_claims
from coordinator_core.session.touch_record import VERB_TOUCH, decode_line, iter_complete_lines, sink_path

_SESSION_ID = "issue50-junk-token-probe"


def _repo(tmp_path) -> str:
    root = tmp_path / "repo"
    os.makedirs(root / ".git")
    return str(root)


def _touched_paths(root, session_id=_SESSION_ID) -> set:
    sink = sink_path(os.path.join(root, ".git", "coordinator-sessions", session_id))
    if not sink.exists():
        return set()
    raw = sink.read_bytes()
    return {
        decode_line(line).path
        for line in iter_complete_lines(raw)
        if decode_line(line).verb == VERB_TOUCH
    }


@pytest.mark.parametrize(
    "cmd, junk_token",
    [
        ("cp a.py $f", "$f"),
        ("cp a.py coordinator/bin/$f", "coordinator/bin/$f"),
        ("cp a.py state/mise-inventory/$RUN.md", "state/mise-inventory/$RUN.md"),
        ("cp a.py f.py 2>&1", "2>&1"),
        ("cp a.py f.py 2>/dev/null", "2>/dev/null"),
    ],
    ids=["bare-var", "prefixed-var", "nested-var", "glued-redirect-fd", "glued-redirect-devnull"],
)
def test_unexpanded_variable_and_glued_redirect_tokens_are_never_claimed(
    tmp_path, cmd, junk_token
):
    root = _repo(tmp_path)
    record_write_claims(cmd, _SESSION_ID, root, denied=False)
    touched = _touched_paths(root)
    assert junk_token not in touched, f"{cmd!r} claimed the junk token {junk_token!r}: {touched}"


def test_directory_target_is_never_claimed(tmp_path):
    root = _repo(tmp_path)
    os.makedirs(os.path.join(root, "state", "mise-inventory"))
    record_write_claims("mkdir state/mise-inventory", _SESSION_ID, root, denied=False)
    assert "state/mise-inventory" not in _touched_paths(root)


def test_real_destination_still_claimed_alongside_a_junk_redirect(tmp_path):
    """A junk token in the same command must not suppress the real claim --
    only the junk token itself is rejected. Uses a redirect-operator shape
    (`echo ... > f.py`, extracted via the `>`-operator branch) rather than
    `cp`'s own "last positional is the destination" rule, since a glued
    `2>&1` trailing a `cp` invocation is mis-attributed as the destination
    by the shared extractor itself (a pre-existing, out-of-footprint
    limitation of `bump_outside_repo_write`'s own positional-argument rule,
    not this module's filter)."""
    root = _repo(tmp_path)
    record_write_claims("echo hi > f.py 2>&1", _SESSION_ID, root, denied=False)
    touched = _touched_paths(root)
    assert touched == {"f.py"}, touched


def test_real_destination_still_claimed_alongside_an_unexpanded_sibling(tmp_path):
    root = _repo(tmp_path)
    record_write_claims("cp a.py f.py && cp a.py $f", _SESSION_ID, root, denied=False)
    touched = _touched_paths(root)
    assert touched == {"f.py"}, touched
